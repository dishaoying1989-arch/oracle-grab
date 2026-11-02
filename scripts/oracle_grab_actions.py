# -*- coding: utf-8 -*-
"""
甲骨文抢机脚本 v3（GitHub Actions 版 · 全天多轮 + 原地升配）
==================================================================
与本地版的分工：
    本脚本在 GitHub Actions 云端运行，电脑关机/卸载 WorkBuddy 均不影响。
    由 workflow 每小时拉起一次，每次 run 内做 N 轮尝试后退出，
    下一整点由 cron 再次拉起 —— 全天不间断，且永远不超出私有仓库分钟额度。

核心策略（与本地 v3 一致）：
    1. 无 ARM 实例 -> 抢保底 1核1G（新建，1核6G 排不上时的兜底）
    2. 已有实例未顶配 -> **原地升配**（update_instance 改 shape_config，不删机重建）
       在线改配失败且提示需停机 -> STOP -> 改配 -> START（公网IP/启动盘保留）
    3. 已达顶配 4核24G -> 只巡检 + 日报
    4. 升配失败（容量不足）机器毫发无损，下一轮继续蹲

配置全部来自 GitHub Secrets（环境变量）：
    OCI_CONFIG_JSON   user/fingerprint/tenancy/region 的 JSON
    OCI_PRIVATE_KEY   私钥全文（PEM）
    SERVERCHAN_KEY    Server酱 SendKey
    SSH_PUB_KEY       SSH 公钥（写入实例元数据）

用法：
    python oracle_grab_actions.py                 # 默认 ROUNDS=3 轮
    python oracle_grab_actions.py --rounds 5      # 指定轮数
    python oracle_grab_actions.py --dry-run       # 只查询+打印，不创建/改配/推送
    python oracle_grab_actions.py --status        # 只查状态并推送
"""

import os
import sys
import json
import time
import random
import datetime
import tempfile
import traceback

# 北京时区（Actions runner 是 UTC，日报判断必须用它）
TZ_BJ = datetime.timezone(datetime.timedelta(hours=8))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

RUN = {
    "rounds": 0, "create_try": 0, "resize_try": 0, "success": 0,
    "capacity": 0, "rate_limit": 0, "network": 0, "error": 0,
}

# 跨 run 状态（cache 挂载点）；必须尽早定义 —— 下面的 CAPPED_TIER_FILE 在模块加载期就会引用它
STATE_DIR = os.environ.get("GRAB_STATE_DIR", ".grab_state")

# ---------------- 配置（全部来自环境变量） ----------------
SEND_KEY = os.environ.get("SERVERCHAN_KEY", "").strip()
PUSH_URL = "https://sctapi.ftqq.com/{}.send".format(SEND_KEY) if SEND_KEY else ""

OCI_CONFIG_JSON = os.environ.get("OCI_CONFIG_JSON", "")
OCI_PRIVATE_KEY = os.environ.get("OCI_PRIVATE_KEY", "")
SSH_PUB_KEY = os.environ.get("SSH_PUB_KEY", "").strip()

def _tenancy_from_cfg():
    """从 OCI_CONFIG_JSON 里取 tenancy（根 compartment）—— 避免把租户 OCID 硬编码进公开仓库。"""
    try:
        return (json.loads(OCI_CONFIG_JSON) or {}).get("tenancy", "").strip()
    except Exception:
        return ""


# 优先用 Secret OCI_COMPARTMENT_ID；否则用配置文件里的 tenancy（根 compartment 即租户）
COMPARTMENT_ID = os.environ.get("OCI_COMPARTMENT_ID", "").strip() or _tenancy_from_cfg()
AVAILABILITY_DOMAIN = "QKbr:AP-OSAKA-1-AD-1"
# 子网 OCID 不是凭据（任何操作仍需 API 私钥签名），保留回退值以便开箱即用；
# 可用 Secret OCI_SUBNET_ID 覆盖。
SUBNET_ID = os.environ.get("OCI_SUBNET_ID", "").strip() or \
    "ocid1.subnet.oc1.ap-osaka-1.aaaaaaaarlrg6qcpgpm4icz3hb3kix5aix7s74f5ej5vgqz4pop5h5un47kq"
ARM_IMAGE_ID = "ocid1.image.oc1.ap-osaka-1.aaaaaaaafjymvqo2bqj5uur45dfvhch6sp72yedb7xfaenkbvufx5zisnmya"  # 回退值
BOOT_VOLUME_GB = 50

FREE_TOTAL_OCPUS = 2
FREE_TOTAL_MEM_GB = 12
# 2026-11-02：不再硬编码 4C/24G。Oracle 2026-06-15 把免费档砍到 2C/12G，
# 本账号 Limits API 实测 standard-a1-core-count=2 / standard-a1-memory-count=12，
# 配额 available 也是满的（2 核 / 12G，说明额度没被占、不是配额问题）。
# 下面这两个是启动时的实测缓存，由 refresh_free_quota() 填充，避免每次都打 Limits API。
_FREE_QUOTA_CACHE = {}
FREE_QUOTA_TTL = 3600          # 实测配额缓存有效期（秒）

TIERS = [
    {"ocpus": 1, "memory_gb": 1,  "label": "1核1G"},
    {"ocpus": 1, "memory_gb": 6,  "label": "1核6G"},
    {"ocpus": 2, "memory_gb": 12, "label": "2核12G"},
    {"ocpus": 4, "memory_gb": 24, "label": "4核24G"},
]
ARM_SHAPE = "VM.Standard.A1.Flex"

# ★ 2026-11-02 新增：多档位轮换探测（PROBE_TIERS）
#
# 为什么需要：原实现每轮只试 TIERS[0]（1核1G）一档，命中面最窄。
# 实测全历史（298 个 run / 2757 次 create）成功率 0 —— 但这不能证明"档位没用"，
# 因为始终只试了 1核1G 这一个组合，从没试过 1核6G / 2核12G。
# A1.Flex 是 flexible shape，同一个 AD 下的可用容量是按 (ocpus, memory) 组合
# 分桶的（官方 shapeConfigOptions 明确列出各组合），不同组合命中不同分桶。
#
# 为什么顺序是 1核1G → 1核6G → 2核12G：
#   1核1G 占的物理资源最少 → 最容易排上（社区通用经验：小规格库存相对充裕）；
#   2核12G 才是这个账号真正想要的档位（实测 A1 限额就是 2核12G），
#   但它要的整块容量最大，最难排上。
# 所以每轮按顺序轮换尝试，用 PROBE_TIER_IDX 记录进度，跨棒保留。
#
# 注意：这不增加 API 调用量 —— 仍然是每轮一次 create，只是换 shape_config，
# 所以不会加剧 429；命中哪档就直接建哪档，之后照旧走原地升配阶梯。
PROBE_TIERS = [0, 1, 2]             # 参与轮换的 TIERS 索引（不含 4核24G：实测限额只有 2核12G）
PROBE_TIER_IDX = 0                  # 当前轮到第几档
PROBE_TIER_FILE = os.path.join(STATE_DIR, "probe_tier.json")

# 多 AD 轮换（开源最佳实践：大阪/东京等多数区域有 3 个 AD，容量可能只出现在某一个）。
# 本账号实测只有 1 个 AD（AD-1），所以是 no-op；但代码保留轮换能力，换区域/换账号即自动生效。
AD_LIST = []
AD_IDX = 0

# 账户 A1 免费额度上限探测（应对 Oracle 2026-06-15 把免费档从 4C/24G 砍到 2C/12G 的政策）。
# CAPPED_TIER = 不可超过的最高 tier 索引；默认 len(TIERS)（不封顶）。
# 一旦某次新建/升配因「service limits were exceeded（standard-a1-core/memory-count）」失败，
# 即说明该账户免费档到不了这一档（纯免费档被砍），把 CAPPED_TIER 压到这一档，
# 之后不再浪费尝试，并写入 account_cap.json 跨日保留（额度是账户级属性，不会天天变）。
CAPPED_TIER_FILE = os.path.join(STATE_DIR, "account_cap.json")
CAPPED_TIER = len(TIERS) - 1          # 允许尝试的最高 tier 索引（默认顶配；撞 limit 后下调，最低 -1=账户免A1额度为0）
CAP_TS = 0                            # account_cap.json 的写入时间（epoch 秒），用于定期复核
# 复核周期：判定"账户免 A1 额度为 0"（cap=-1）之后，每隔这么多小时用 Limits API 实测一次。
# 起因（2026-10-28 踩坑）：某一轮紧跟 429 之后的 limit 报错被误判成"额度为 0"，永久封顶写进
# account_cap.json 后整条链只剩巡检、再也没创建过实例 —— 而实测账号其实还有 2核/12G 免费额度。
# 单次瞬时 limit 不该把链路永久废掉，所以加定期复核 + 现场实测再下结论。
CAP_REPROBE_HOURS = 6
_LIMITS_CLIENT = None                 # Limits 客户端（惰性创建，仅用于只读配额探测）
CFG = None                            # OCI 配置（init_clients 里落库，供 Limits 探测复用）

# 2026-11-02：封顶档位的**默认值**原来一直是 len(TIERS)-1 = 4核24G，
# 但这个账号的实测限额是 2核/12G —— 4核24G 永远创建不出来（只会白撞 limit）。
# 现在默认封顶到「实测额度能支撑的最高档」，并在拿到实测额度后动态收窄。
def _default_cap_tier():
    """按实测额度（2核12G）推导默认封顶档位：能整档装下就允许，否则退一档。"""
    best = 0
    for i, t in enumerate(TIERS):
        if t["ocpus"] <= FREE_TOTAL_OCPUS and t["memory_gb"] <= FREE_TOTAL_MEM_GB:
            best = i
    return best

CAPPED_TIER = _default_cap_tier()

ROUNDS = 3                       # 单次 run 内尝试轮数（workflow 可通过输入覆盖）
ROUND_GAP_CREATE = 45            # 无实例时轮间隔**基准值**（自检/回退用；运行时走下面的自适应间隔）
ROUND_GAP_RESIZE = 150           # 有实例时轮间隔
ROUND_GAP_IDLE = 420             # 顶配巡检间隔

# ---- 自适应轮间隔（AIMD 思路：撞 429 就退让，长时间不撞再慢慢提速）----
# 起因：固定 45s 时实测每轮要撞 ~1.8 次 429，棒内 70% 的时间耗在退避等待上，
#       既没换来更多有效探测，又一直把账号往"限死/封号"的方向顶。
# 机制：维护 429 命中率的指数滑动平均（EWMA）：
#       - 高于 GAP_HI -> 乘性拉大间隔（反应快，立刻让 OCI 喘口气）
#       - 低于 GAP_LO -> 乘性缩短间隔（小步提速，去试探真实上限）
#       - 介于两者之间 -> 保持不动（避免在阈值附近来回抖）
# 自适应值会写进 .grab_state/adaptive_gap.json 跨棒保留，不用每棒从头试探。
#
# ★ 2026-11-02 修复（两个真 bug，均已实测确认）：
#   bug1: 轮末返回给 adapt_gap 的 fail["type"] 永远是 "capacity"（429 在 call_with_rate_limit
#         内部就被重试消化掉了），所以「退让」分支一次都没触发过 —— 实测 run #296 里
#         RATE-LIMIT 出现 45 次，[GAP] 退让/提速行 0 次，间隔全程钉死在 90s 下限。
#         现在改用轮级标志 ROUND_HIT_RATE_LIMIT 传递真实 429 命中。
#   bug2: 上面注释描述的是「429 命中率 EWMA + 阈值带」，但原实现其实是「连续 N 轮没撞就提速」
#         的计数器 —— 两者语义完全不同，注释与实现不符。计数器在 429 偶发时会把间隔一路
#         拉到下限（90s），等于没有自适应。现在按注释实现真正的 EWMA + 阈值带。
GAP_INIT_CREATE = 180            # 自适应起始间隔（秒）
GAP_MIN_CREATE = 90              # 下限：再顺畅也不快过 90s，给账号留安全边际
GAP_MAX_CREATE = 600             # 上限：限流再重也最多退到 10 分钟一轮
GAP_MULT_UP = 1.35               # 退让倍数（撞 429 就拉大间隔，反应要快）
GAP_MULT_DOWN = 0.90             # 提速倍数（缩短间隔，小步试探）
GAP_UP_COOLDOWN = 5              # 连续多少轮"无 429"才提速一次（防抖、防冒进）
GAP_EWMA_ALPHA = 0.30            # EWMA 平滑系数（0.3 ≈ 最近 3~4 轮的影响）
GAP_HI = 0.35                    # 429 命中率 > 35% -> 退让
GAP_LO = 0.10                    # 429 命中率 < 10% -> 提速（带内不动，防抖）

# 接力模式：单棒最长运行秒数（0=不限）。GitHub 单个 job 上限 6 小时，
# workflow 传 19500（约 5.4 小时）跑满后优雅退场，再由 workflow 触发下一棒，
# 从而绕开 GitHub 那个不投递的 cron 调度器。
MAX_SECONDS = 0
START_TS = time.time()
REPORT_SENT_DATE = ""            # 长棒模式下防止日报重复发
POLL_INTERVAL = 15
WAIT_RUNNING_TIMEOUT = 420       # Actions 里单 run 有时长预算，等 RUNNING 上限 7 分钟
STATE_WAIT_TIMEOUT = 240
REPORT_HOUR_BJ = 2               # 北京时间每天 **02:00** 云端发「战报 + 体检」合并日报（每天只此一条）。
                                 # 老板决定：电脑关机也要收到推送 → 只有跑在 GitHub 上的这条能做到，
                                 # 本机 WorkBuddy 的体检自动化改为**静默巡检**（只修问题，不发日常推送）。
                                 # 原因是 Server酱 Turbo 免费额度每天仅 5 条：
                                 #   固定占 1 条，剩 4 条留给"抢到机/升配成功/配置错误"等即时事件，
                                 #   否则真抢到的那条可能被当日额度挤掉，反而收不到。
                                 # 日报内容已包含链路体检（见 chain_health()），所以不需要第二条。
                                 # 想彻底关掉日报：把这里改成 -1。
                                 # 抢到机 / 升配成功 / 配置错误这类**事件推送**不受此开关影响，永远即时推。
# （STATE_DIR 已在上方「配置」段之前定义，这里不再重复）

ALIVE_STATES = ("RUNNING", "STOPPED", "STARTING", "STOPPING", "PROVISIONING")
DEAD_STATES = ("TERMINATED", "TERMINATING")
CAPACITY_KEYWORDS = ("out of host capacity", "out of capacity", "capacity",
                     "insufficient capacity", "no capacity", "host capacity")
NEED_STOP_KEYWORDS = ("must be stopped", "incorrectstate", "instance must be in stopped",
                      "cannot update shape", "stopped state")

DRY_RUN = "--dry-run" in sys.argv
STATUS_ONLY = "--status" in sys.argv


# ---------------- 工具 ----------------
def now_bj():
    return datetime.datetime.now(TZ_BJ).strftime("%Y-%m-%d %H:%M:%S")


def jitter(base, spread=0.25):
    return int(base * (1 + random.uniform(-spread, spread)))


def current_ad():
    """当前轮使用的可用域：在 AD_LIST 上按 AD_IDX 轮换（开源最佳实践，撞容量不足就换个 AD 再试）。"""
    global AD_IDX
    if AD_LIST:
        return AD_LIST[AD_IDX % len(AD_LIST)]
    return AVAILABILITY_DOMAIN


def rotate_ad():
    """容量不足时切到下一个 AD（无货的 AD 再怎么蹲也出不来容量）。"""
    global AD_IDX
    if len(AD_LIST) > 1:
        AD_IDX = (AD_IDX + 1) % len(AD_LIST)
        print("[AD] 容量不足，轮换到 {}".format(current_ad()))


def push(title, desp):
    if not PUSH_URL:
        print("[PUSH SKIP] SERVERCHAN_KEY 未配置")
        return False
    import requests
    try:
        resp = requests.post(PUSH_URL, data={"title": title, "desp": desp}, timeout=30)
        print("[PUSH] HTTP {} | {}".format(resp.status_code, resp.text[:150]))
        try:
            ok = resp.json().get("code") == 0
        except Exception:
            ok = resp.status_code == 200
        print("[PUSH OK]" if ok else "[PUSH WARN]", title)
        return ok
    except Exception as e:
        print("[PUSH FAILED]", e)
        return False


def is_rate_limited(err):
    if err.__class__.__module__.startswith("oci") and err.__class__.__name__ == "ServiceError":
        if getattr(err, "status", None) == 429:
            return True
        return (err.code or "").upper() in ("LIMITEXCEEDED", "SERVICELIMITEXCEEDED", "TOOMANYREQUESTS")
    return False


def get_retry_after(err):
    try:
        headers = getattr(err, "headers", None) or {}
        for key in ("Retry-After", "opc-retry-after"):
            val = headers.get(key)
            if val:
                return int(val)
    except Exception:
        pass
    return None


# 最近一次 429 时 OCI 在响应头里给出的退避秒数（供 AIMD 直接采纳）
LAST_RETRY_AFTER = None


def rate_limit_wait(err, attempt):
    # Actions 单 run 有 6 小时预算；退避上限收紧到 120s，单轮最多重试 ~6 分钟
    global LAST_RETRY_AFTER
    wait = get_retry_after(err)
    LAST_RETRY_AFTER = wait
    if wait is None:
        wait = min(30 * (2 ** attempt), 120)
    wait = min(int(wait), 120)
    RUN["rate_limit"] += 1
    print("[RATE-LIMIT] 等待 {}s{}".format(wait, " (Retry-After)" if LAST_RETRY_AFTER else ""))
    time.sleep(wait)
    return wait


# 每一轮内是否撞过 429。
# 【2026-11-02 修复的真实 bug】原来 429 只体现在 RUN["rate_limit"] 计数上，
# 而 adapt_gap() 的入参是 fail["type"] == "rate_limit" —— 但 call_with_rate_limit()
# 在轮内就把 429 重试消化掉了，轮末返回给 do_round 的 fail 已经是 "capacity"，
# 于是 adapt_gap 永远收到 hit_rate_limit=False，AIMD 的「退让」分支一次都没触发过
# （2026-11-02 实测 run #296：RATE-LIMIT 出现 45 次，[GAP] 退让/提速行 0 次，
#   轮间隔全程钉死在 90s 下限）。现在用轮级标志把真实 429 命中率传给 AIMD。
ROUND_HIT_RATE_LIMIT = False


def call_with_rate_limit(func, *args, max_attempts=2, **kwargs):
    global ROUND_HIT_RATE_LIMIT
    attempt = 0
    while True:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if is_rate_limited(e) and attempt < max_attempts:
                ROUND_HIT_RATE_LIMIT = True
                rate_limit_wait(e, attempt)
                attempt += 1
                continue
            raise


def is_network_error(err):
    low = str(err).lower()
    return any(k in low for k in ("max retries exceeded", "unexpected_eof", "timed out",
                                  "connection", "tls", "unreachable", "read timeout"))


def call_with_net_retry(func, *args, max_attempts=4, base_sleep=5, **kwargs):
    attempt = 0
    while True:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if is_network_error(e) and attempt < max_attempts - 1:
                print("[NET-RETRY {}] {}".format(attempt + 1, str(e)[:120]))
                time.sleep(base_sleep * (attempt + 1))
                attempt += 1
                continue
            raise


def classify_error(e):
    if e.__class__.__name__ == "ServiceError":
        msg = "{} | code={} | status={}".format(e.message, e.code, e.status)
        low = (str(e.message) or "").lower()
        code = (e.code or "").upper()
        if e.status == 429 or "toomany" in code.lower() or "throttle" in code.lower():
            return "rate_limit", msg
        if any(k in low for k in CAPACITY_KEYWORDS):
            return "capacity", msg
        # 配额到顶：账户免费档到不了这个规格（最常见是 2026-06-15 政策后纯免费档封顶 2C/12G）。
        # 与「暂时没货」(capacity) 必须分开 —— 前者是硬限制，再怎么重试也没用，应封顶。
        if ("service limits were exceeded" in low
                or "service limit" in low
                or "standard-a1-core-count" in low
                or "standard-a1-memory-count" in low
                or "limit exceeded" in low
                or code == "LIMITEXCEEDED"):
            return "limit", msg
        if any(k in low for k in NEED_STOP_KEYWORDS):
            return "need_stop", msg
        if "quota" in low:
            return "quota", msg
        if e.status in (401, 404):
            return "config", msg
        return "error", msg
    if is_network_error(e):
        return "network", str(e)
    return "error", str(e)


# ---------------- 自适应轮间隔 ----------------
GAP_CREATE = GAP_INIT_CREATE
_gap_clean = 0
_gap_ewma = None            # 429 命中率的 EWMA（None = 本棒还没样本）
GAP_FILE = os.path.join(STATE_DIR, "adaptive_gap.json")


def _load_gap():
    try:
        with open(GAP_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        v = float(d.get("gap_create", GAP_INIT_CREATE))
        # 2026-11-02：把 EWMA 也跨棒恢复，避免每棒开头重新"学习"限流水位
        ew = d.get("ewma")
        global _gap_ewma
        if ew is not None:
            try:
                _gap_ewma = min(max(float(ew), 0.0), 1.0)
            except Exception:
                _gap_ewma = None
        return min(max(v, GAP_MIN_CREATE), GAP_MAX_CREATE)
    except Exception:
        return GAP_INIT_CREATE


def _save_gap(v):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(GAP_FILE, "w", encoding="utf-8") as f:
            json.dump({"gap_create": round(float(v), 1), "updated": now_bj(),
                       "ewma": (round(float(_gap_ewma), 4) if _gap_ewma is not None else None)},
                      f, ensure_ascii=False)
    except Exception:
        pass


def _load_probe_tier():
    try:
        with open(PROBE_TIER_FILE, "r", encoding="utf-8") as f:
            v = int(json.load(f).get("probe_tier_idx", 0))
        return max(0, min(v, len(PROBE_TIERS) - 1))
    except Exception:
        return 0


def _save_probe_tier(v):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(PROBE_TIER_FILE, "w", encoding="utf-8") as f:
            json.dump({"probe_tier_idx": int(v), "updated": now_bj()}, f, ensure_ascii=False)
    except Exception:
        pass


def _load_cap():
    """读取账户 A1 免费档上限（跨日保留；额度是账户级属性，但 Oracle 会调整/回收）。"""
    global CAPPED_TIER, CAP_TS
    try:
        with open(CAPPED_TIER_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        v = int(d.get("capped_tier", len(TIERS)))
        CAPPED_TIER = min(max(v, -1), len(TIERS) - 1)
        try:
            CAP_TS = int(d.get("ts") or 0)
        except Exception:
            CAP_TS = 0
    except Exception:
        CAPPED_TIER = len(TIERS) - 1
        CAP_TS = 0
    return CAPPED_TIER


def _save_cap(v):
    global CAPPED_TIER, CAP_TS
    CAPPED_TIER = min(max(int(v), -1), len(TIERS) - 1)
    CAP_TS = int(time.time())
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(CAPPED_TIER_FILE, "w", encoding="utf-8") as f:
            json.dump({"capped_tier": CAPPED_TIER, "updated": now_bj(), "ts": CAP_TS,
                       "label": "无（账户免费 A1 额度为 0）" if CAPPED_TIER < 0
                       else TIERS[CAPPED_TIER]["label"]},
                      f, ensure_ascii=False)
    except Exception:
        pass


def refresh_free_quota():
    """用 Limits API 实测账户 A1 免费额度上限（只读，不消耗 create 限流）。
    2026-11-02 新增：以前 quota_text() 硬编码 4C/24G，但真实值是 2C/12G ——
    日报里一直显示错的额度，会误导"是不是额度用完了"的判断。"""
    global FREE_TOTAL_OCPUS, FREE_TOTAL_MEM_GB, _LIMITS_CLIENT
    try:
        import oci
        if _LIMITS_CLIENT is None:
            if not CFG:
                return None
            _LIMITS_CLIENT = oci.limits.LimitsClient(CFG)
            try:
                _LIMITS_CLIENT.retry_strategy = oci.retry.NoneRetryStrategy()
            except Exception:
                pass
        ad = current_ad()
        # ★ 重要口径区别（2026-11-02 实测确认）：
        #   get_resource_availability().available = **剩余可分配量**（会随实例占用而变）
        #   list_limit_values(...).value        = **限额上限**（恒定 2 / 12）
        # 上限要用 list_limit_values 取；available 只用来判断"额度是不是真的被占满了"。
        try:
            vals = call_with_net_retry(
                _LIMITS_CLIENT.list_limit_values,
                compartment_id=COMPARTMENT_ID, service_name="compute",
                name="standard-a1-core-count").data
            if vals:
                FREE_TOTAL_OCPUS = int(vals[0].value)
        except Exception:
            pass
        try:
            vals = call_with_net_retry(
                _LIMITS_CLIENT.list_limit_values,
                compartment_id=COMPARTMENT_ID, service_name="compute",
                name="standard-a1-memory-count").data
            if vals:
                FREE_TOTAL_MEM_GB = int(vals[0].value)
        except Exception:
            pass
        # 顺带看剩余量（额度被占满时 available 会小于上限）
        avail_c = None
        try:
            avail_c = call_with_net_retry(
                _LIMITS_CLIENT.get_resource_availability,
                service_name="compute", limit_name="standard-a1-core-count",
                compartment_id=COMPARTMENT_ID, availability_domain=ad).data.available
        except Exception:
            pass
        _FREE_QUOTA_CACHE.clear()
        _FREE_QUOTA_CACHE.update({"cores": FREE_TOTAL_OCPUS, "mem": FREE_TOTAL_MEM_GB,
                                   "ts": time.time()})
        print("[QUOTA] 实测 A1 免费额度上限：{}核/{}G（当前剩余 {} 核）".format(
            FREE_TOTAL_OCPUS, FREE_TOTAL_MEM_GB,
            avail_c if avail_c is not None else "?"))
        # 实测额度既然已知，就把封顶档位收窄到「额度能整档装下的最高档」——
        # 4核24G 在 2核/12G 的账号上永远创建不出来，试它只是白撞 limit。
        want = _default_cap_tier()
        if CAPPED_TIER > want:
            _save_cap(want)
            print("[QUOTA] 按实测额度把封顶档位从 {} 收窄到 {}".format(
                TIERS[CAPPED_TIER]["label"] if CAPPED_TIER >= 0 else "无",
                TIERS[want]["label"]))
        return (FREE_TOTAL_OCPUS, FREE_TOTAL_MEM_GB)
    except Exception as e:
        print("[QUOTA] 额度实测失败，沿用默认值 {}核/{}G: {}".format(
            FREE_TOTAL_OCPUS, FREE_TOTAL_MEM_GB, str(e)[:100]))
        return None


def probe_a1_availability():
    """只读查询账户 A1 免费配额**剩余量**（Limits API：不创建实例、不消耗 create 限流额度）。
    返回 (剩余可用核数, 剩余可用内存GB)；探测失败返回 None。

    口径说明：这里用 get_resource_availability().available（剩余可分配量）是对的 ——
    判断"额度是不是真的被占满了"要看剩余量，而不是限额上限。
    （限额上限由 refresh_free_quota 用 list_limit_values 取，两者不是一回事。）"""
    global _LIMITS_CLIENT
    try:
        import oci
        if _LIMITS_CLIENT is None:
            if not CFG:
                return None
            _LIMITS_CLIENT = oci.limits.LimitsClient(CFG)
            try:
                _LIMITS_CLIENT.retry_strategy = oci.retry.NoneRetryStrategy()
            except Exception:
                pass
        ad = current_ad()
        cores = call_with_net_retry(
            _LIMITS_CLIENT.get_resource_availability,
            service_name="compute", limit_name="standard-a1-core-count",
            compartment_id=COMPARTMENT_ID, availability_domain=ad).data.available
        mem = None
        try:
            mem = call_with_net_retry(
                _LIMITS_CLIENT.get_resource_availability,
                service_name="compute", limit_name="standard-a1-memory-count",
                compartment_id=COMPARTMENT_ID, availability_domain=ad).data.available
        except Exception:
            pass
        return (cores, mem)
    except Exception as e:
        print("[CAP-PROBE] 配额探测失败:", str(e)[:120])
        return None


def maybe_unbrick_cap():
    """cap=-1（曾判定"零额度"）每隔 CAP_REPROBE_HOURS 小时实测复核一次；配额其实还在就解除封顶。
    返回 True 表示本次做了复核并可能改变了封顶值。"""
    global CAPPED_TIER
    if CAPPED_TIER >= 0:
        return False
    if CAP_TS and (time.time() - CAP_TS) < CAP_REPROBE_HOURS * 3600:
        return False
    q = probe_a1_availability()
    if q is None:
        return False
    cores, mem = q
    if (cores or 0) >= 1 and (mem is None or (mem or 0) >= 1):
        _save_cap(len(TIERS) - 1)
        print("[CAP-HEAL] 实测仍有 A1 额度 {}核/{}G → 解除'零额度'封顶，恢复创建".format(cores, mem))
    else:
        _save_cap(-1)
        print("[CAP-HEAL] 实测 A1 额度 {}核/{}G → 维持零额度封顶（{}h 后再复核）".format(
            cores, mem, CAP_REPROBE_HOURS))
    return True


def adapt_gap(hit_rate_limit, retry_after=None):
    """按 429 命中率的 EWMA 自适应轮间隔（真正的 AIMD，带阈值带防抖）。
    retry_after：OCI 在 429 响应头给的退避秒数 —— 它比我们猜的准，优先采纳（封顶在区间内）。

    【2026-11-02 重写】原实现是「连续 N 轮没撞 429 就提速」的计数器，与注释描述的
    EWMA 阈值带完全不符，且轮末拿不到真实 429 信号（见 GAP 段 bug1/bug2 注释）。
    现在每轮更新 EWMA：命中率 > GAP_HI 退让、< GAP_LO 提速、带内保持不动。
    """
    global GAP_CREATE, _gap_clean, _gap_ewma
    # 1) 更新 429 命中率 EWMA（这一轮撞了 = 1，没撞 = 0）
    sample = 1.0 if hit_rate_limit else 0.0
    if _gap_ewma is None:
        _gap_ewma = sample
    else:
        _gap_ewma = (1 - GAP_EWMA_ALPHA) * _gap_ewma + GAP_EWMA_ALPHA * sample

    # 2) 按 EWMA 落在哪条区间决定动作
    if _gap_ewma > GAP_HI:
        _gap_clean = 0
        if retry_after and retry_after > 0:
            new = min(max(retry_after, GAP_MIN_CREATE), GAP_MAX_CREATE)
            act = "退让(遵从Retry-After)"
        else:
            new = min(GAP_CREATE * GAP_MULT_UP, GAP_MAX_CREATE)
            act = "退让"
    elif _gap_ewma < GAP_LO:
        _gap_clean += 1
        if _gap_clean < GAP_UP_COOLDOWN:
            return
        _gap_clean = 0
        new = max(GAP_CREATE * GAP_MULT_DOWN, GAP_MIN_CREATE)
        act = "提速"
    else:
        # 带内：保持当前间隔，但清掉提速冷却计数（一旦出了带才有资格提速）
        _gap_clean = 0
        return

    if abs(new - GAP_CREATE) >= 1:
        print("[GAP] {}（429率 {:.0%}） → 轮间隔 {}s -> {}s".format(
            act, _gap_ewma, int(GAP_CREATE), int(new)))
        GAP_CREATE = new
        _save_gap(new)


# ---------------- 跨 run 状态（今日累计计数） ----------------
def load_state():
    try:
        with open(os.path.join(STATE_DIR, "daily.json"), "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") == datetime.datetime.now(TZ_BJ).strftime("%Y-%m-%d"):
            return d
    except Exception:
        pass
    return {"date": datetime.datetime.now(TZ_BJ).strftime("%Y-%m-%d"),
            "create_try": 0, "resize_try": 0, "capacity": 0, "success": 0, "runs": 0}


def save_state(st):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        st["runs"] = st.get("runs", 0) + 1
        for k in ("create_try", "resize_try", "capacity", "success"):
            st[k] = st.get(k, 0) + RUN[k]
        with open(os.path.join(STATE_DIR, "daily.json"), "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception as e:
        print("[STATE WRITE FAILED]", e)


def baton_info():
    """接力链健康信息：本棒 run 编号 / 已跑时长 / 距交棒剩余"""
    rnum = os.environ.get("GITHUB_RUN_NUMBER", "-")
    el = int(time.time() - START_TS)
    txt = "本棒 run #{}，已跑 {}h{}m".format(rnum, el // 3600, (el % 3600) // 60)
    if MAX_SECONDS:
        left = max(0, int(MAX_SECONDS - el))
        txt += "，{}h{}m 后交给下一棒".format(left // 3600, (left % 3600) // 60)
    return txt


def daily_summary():
    st = load_state()
    return ("今日累计（北京时间 {}）：已拉起 {} 轮 | 新建 {} 次 / 升配 {} 次 | 成功 {} | 容量不足 {}\n"
            "接力链：{}").format(
        now_bj(), st.get("runs", 0), st.get("create_try", 0), st.get("resize_try", 0),
        st.get("success", 0), st.get("capacity", 0), baton_info())


def _gh_runs(per_page=20):
    """只读查本仓库最近的 Actions run（公开仓库匿名可读；有 GH_PAT 时带鉴权提高限额）"""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        return None
    import requests
    h = {"Accept": "application/vnd.github+json", "User-Agent": "oracle-grab-health"}
    pat = os.environ.get("GH_PAT", "").strip()
    if pat:
        h["Authorization"] = "Bearer " + pat
    r = requests.get("https://api.github.com/repos/{}/actions/runs?per_page={}".format(repo, per_page),
                     headers=h, timeout=25)
    if r.status_code != 200:
        raise RuntimeError("GitHub API HTTP {}".format(r.status_code))
    return (r.json() or {}).get("workflow_runs", []) or []


def chain_health():
    """接力链体检（只读，不创建任何资源）：24h 运行次数/失败数、有无空档、有无重复棒。
    原来这部分由本机 WorkBuddy 自动化负责，现已合并进云端日报 —— 每天只发这一条微信。"""
    if not os.environ.get("GITHUB_REPOSITORY"):
        return "链路体检：非 Actions 环境，跳过"
    try:
        runs = _gh_runs(20)
        now = datetime.datetime.now(datetime.timezone.utc)

        def _t(x):
            return datetime.datetime.strptime(x["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc)

        d24 = [x for x in runs if (now - _t(x)).total_seconds() <= 86400]
        me = str(os.environ.get("GITHUB_RUN_NUMBER", ""))
        dup = [x for x in runs if x.get("status") in ("in_progress", "queued")
               and str(x.get("run_number")) != me]
        fails = [x for x in d24 if x.get("conclusion") == "failure"]

        gap_max, gap_at = 0.0, ""
        ordered = sorted(d24, key=_t)
        for a, b in zip(ordered, ordered[1:]):
            g = (_t(b) - _t(a)).total_seconds()
            if g > gap_max:
                gap_max, gap_at = g, _t(b).astimezone(TZ_BJ).strftime("%d日 %H:%M")

        lines = ["## 链路体检",
                 "- 24h 内共运行 **{}** 次，失败 **{}** 次".format(len(d24), len(fails))]
        if gap_max > 900:
            lines.append("- ⚠ 24h 内最大空档 **{}h{}m**（{} 那次启动前），期间疑似断过链 → "
                         "由体检自动化或人工重启接上".format(
                             int(gap_max) // 3600, (int(gap_max) % 3600) // 60, gap_at))
        else:
            lines.append("- ✅ 接力连续，24h 内最大间隔仅 {} 分钟".format(int(gap_max) // 60))
        if dup:
            lines.append("- ⚠ 除本棒外还有 {} 个 run 在活动（{}），可能重复抢同一 Oracle 账号 → "
                         "建议去 Actions 页面取消多余的".format(
                             len(dup), "、".join("#" + str(x.get("run_number")) for x in dup[:3])))
        lines.append("- 最近运行：" + "、".join(
            "#{}({})".format(x.get("run_number"), x.get("conclusion") or x.get("status"))
            for x in runs[:5]))
        return "\n".join(lines)
    except Exception as e:
        return "## 链路体检\n- 体检失败（不影响抢机）：{}".format(str(e)[:120])


# ---------------- OCI 初始化 ----------------
def init_clients():
    import oci
    global CFG
    if not OCI_CONFIG_JSON or not OCI_PRIVATE_KEY:
        raise RuntimeError("缺少 Secrets：OCI_CONFIG_JSON / OCI_PRIVATE_KEY 未配置")
    if not COMPARTMENT_ID:
        raise RuntimeError("无法确定 compartment：OCI_CONFIG_JSON 缺少 tenancy 字段，请补上或新增 Secret OCI_COMPARTMENT_ID")
    key_file = os.path.join(tempfile.gettempdir(), "oci_api_key.pem")
    with open(key_file, "w", encoding="utf-8") as f:
        f.write(OCI_PRIVATE_KEY.replace("\\n", "\n") if "\\n" in OCI_PRIVATE_KEY else OCI_PRIVATE_KEY)
    try:
        os.chmod(key_file, 0o600)
    except Exception:
        pass
    cfg = json.loads(OCI_CONFIG_JSON)
    cfg["key_file"] = key_file
    oci.config.validate_config(cfg)
    cfg.setdefault("connection_timeout", 30)
    cfg.setdefault("read_timeout", 90)
    CFG = cfg                             # 供 Limits 配额探测复用同一份配置
    compute = oci.core.ComputeClient(cfg)
    identity = oci.identity.IdentityClient(cfg)

    # 关键：关掉 OCI SDK 自带的默认重试/退避。它的默认策略会在单次调用里对 429/5xx
    # 指数退避重试好几分钟，导致我们自己的 AIMD 节奏形同虚设、日志看起来像卡死。
    # 关掉后所有重试都由我们的 call_with_rate_limit / call_with_net_retry 显式掌控。
    try:
        compute.retry_strategy = oci.retry.NoneRetryStrategy()
        identity.retry_strategy = oci.retry.NoneRetryStrategy()
    except Exception:
        pass

    # 自动探测 AD 与最新 Ubuntu 24.04 ARM 镜像（失败回退硬编码）
    global AVAILABILITY_DOMAIN, ARM_IMAGE_ID, AD_LIST
    try:
        names = [a.name for a in call_with_net_retry(
            identity.list_availability_domains, compartment_id=COMPARTMENT_ID).data]
        AD_LIST = list(names)
        if names:
            AVAILABILITY_DOMAIN = names[0]
            print("[AD] 探测到 {} 个 AD，启用轮换：{}".format(len(names), "、".join(names)))
    except Exception as e:
        print("[AD] 探测失败，用回退值:", str(e)[:100])
    if not AD_LIST:
        AD_LIST = [AVAILABILITY_DOMAIN]
    try:
        for im in call_with_net_retry(
                compute.list_images, compartment_id=COMPARTMENT_ID,
                operating_system="Canonical Ubuntu", shape=ARM_SHAPE,
                sort_by="TIMECREATED", sort_order="DESC").data:
            dn = im.display_name or ""
            if im.lifecycle_state == "AVAILABLE" and "aarch64" in dn and "24.04" in dn and "Minimal" not in dn:
                ARM_IMAGE_ID = im.id
                print("[IMAGE]", dn)
                break
    except Exception as e:
        print("[IMAGE] 探测失败，用回退值:", str(e)[:100])
    return compute


# ---------------- 实例操作 ----------------
def list_arm_instances(compute):
    result = []
    for inst in call_with_net_retry(compute.list_instances, compartment_id=COMPARTMENT_ID).data:
        if inst.shape != ARM_SHAPE or inst.lifecycle_state in DEAD_STATES:
            continue
        result.append({
            "id": inst.id, "display_name": inst.display_name, "state": inst.lifecycle_state,
            "ocpus": inst.shape_config.ocpus if inst.shape_config else 0,
            "memory_gb": inst.shape_config.memory_in_gbs if inst.shape_config else 0,
        })
    return result


def get_current_tier(arms):
    if not arms:
        return None
    best = max(arms, key=lambda x: (x["ocpus"], x.get("memory_gb") or 0))
    for i, t in enumerate(TIERS):
        # 必须同时匹配核数与内存：TIERS 里 1核1G 与 1核6G 核数相同，
        # 只比核数会把 1核6G 实例误判成 1核1G（2026-10-30 复核发现）。
        if abs(best["ocpus"] - t["ocpus"]) < 0.5 and \
                abs((best.get("memory_gb") or 0) - t["memory_gb"]) < 0.5:
            return i, best
    # 内存对不上时退回按核数匹配（老实例可能缺 memory_gb 字段）
    for i, t in enumerate(TIERS):
        if abs(best["ocpus"] - t["ocpus"]) < 0.5:
            return i, best
    return None


def quota_text(arms):
    # 2026-11-02：免费额度不再硬编码 4C/24G（Oracle 2026-06-15 已砍到 2C/12G，
    # 且本账号 Limits API 实测就是 standard-a1-core-count=2 / memory-count=12）。
    # 改为优先用实测值；探测不到时回落到实测确认的 2C/12G。
    total_c, total_g = FREE_TOTAL_OCPUS, FREE_TOTAL_MEM_GB
    if _FREE_QUOTA_CACHE and _FREE_QUOTA_CACHE.get("ts"):
        age = time.time() - _FREE_QUOTA_CACHE["ts"]
        if age < FREE_QUOTA_TTL:
            total_c = _FREE_QUOTA_CACHE.get("cores", total_c)
            total_g = _FREE_QUOTA_CACHE.get("mem", total_g)
    return "{}C/{}G（免费额度 {}C/{}G）".format(
        sum(a["ocpus"] for a in arms), sum(a["memory_gb"] for a in arms),
        total_c, total_g)


def wait_for_state(compute, instance_id, target, timeout=STATE_WAIT_TIMEOUT):
    deadline = time.time() + timeout
    state = "UNKNOWN"
    while time.time() < deadline:
        try:
            state = call_with_net_retry(compute.get_instance, instance_id).data.lifecycle_state
            if state == target:
                return True, state
            if state in DEAD_STATES or state == "FAILED":
                return False, state
        except Exception as e:
            print("[state warn]", str(e)[:100])
        time.sleep(POLL_INTERVAL)
    return False, state


def get_instance_public_ip(compute, instance_id):
    try:
        for va in call_with_net_retry(
                compute.list_vnic_attachments, compartment_id=COMPARTMENT_ID,
                instance_id=instance_id).data:
            try:
                v = call_with_net_retry(compute.get_vnic, va.vnic_id).data
                if v.public_ip:
                    return v.public_ip
            except Exception:
                continue
    except Exception as e:
        print("[ip warn]", str(e)[:100])
    return None


def action_create(compute, tier, dry_run=False):
    import oci
    RUN["create_try"] += 1
    if dry_run:
        print("[DRY-RUN] 将创建 {}C/{}G".format(tier["ocpus"], tier["memory_gb"]))
        return "dry-run-id", "dry-run", None
    details = oci.core.models.LaunchInstanceDetails(
        compartment_id=COMPARTMENT_ID,
        availability_domain=current_ad(),
        display_name="arm-a1-{}ocpu-{}gb".format(tier["ocpus"], tier["memory_gb"]),
        shape=ARM_SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=tier["ocpus"], memory_in_gbs=tier["memory_gb"]),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            source_type="image", image_id=ARM_IMAGE_ID, boot_volume_size_in_gbs=BOOT_VOLUME_GB),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=SUBNET_ID, assign_public_ip=True),
        metadata={"ssh_authorized_keys": SSH_PUB_KEY},
    )
    try:
        resp = call_with_rate_limit(compute.launch_instance, details)
        return resp.data.id, resp.data.display_name, None
    except Exception as e:
        ftype, fmsg = classify_error(e)
        return None, None, {"type": ftype, "message": fmsg}


def action_resize_online(compute, inst_id, target, dry_run=False):
    import oci
    RUN["resize_try"] += 1
    if dry_run:
        print("[DRY-RUN] 将原地升配 -> {}".format(target["label"]))
        return True, None
    details = oci.core.models.UpdateInstanceDetails(
        shape_config=oci.core.models.UpdateInstanceShapeConfigDetails(
            ocpus=target["ocpus"], memory_in_gbs=target["memory_gb"]))
    try:
        call_with_rate_limit(compute.update_instance, inst_id, details)
        return True, None
    except Exception as e:
        ftype, fmsg = classify_error(e)
        return False, {"type": ftype, "message": fmsg}


def action_resize_via_stop(compute, inst_id, target):
    """停机改配再开机。无论成功失败都把机器开回去。"""
    try:
        call_with_rate_limit(compute.instance_action, inst_id, "STOP")
        ok, state = wait_for_state(compute, inst_id, "STOPPED")
        if not ok:
            return False, {"type": "error", "message": "无法停机({})".format(state)}
    except Exception as e:
        ftype, fmsg = classify_error(e)
        return False, {"type": ftype, "message": "STOP 失败：" + fmsg}
    import oci
    try:
        details = oci.core.models.UpdateInstanceDetails(
            shape_config=oci.core.models.UpdateInstanceShapeConfigDetails(
                ocpus=target["ocpus"], memory_in_gbs=target["memory_gb"]))
        call_with_rate_limit(compute.update_instance, inst_id, details)
    except Exception as e:
        ftype, fmsg = classify_error(e)
        print("[RESIZE] 改配失败，恢复开机:", str(fmsg)[:120])
        try:
            compute.instance_action(inst_id, "START")
        except Exception as e2:
            push("甲骨文抢机：实例停在关机状态，需人工处理",
                 "实例 {} 改配失败且开机失败：{}".format(inst_id, str(e2)[:300]))
        return False, {"type": ftype, "message": fmsg}
    try:
        compute.instance_action(inst_id, "START")
    except Exception as e:
        return False, {"type": "error", "message": "改配已提交但开机失败：" + str(e)}
    return True, None


def build_instance_table(compute):
    lines = ["| 显示名 | 形状 | OCPU | 内存 | 状态 | 公网IP |",
             "| --- | --- | --- | --- | --- | --- |"]
    try:
        for inst in call_with_net_retry(compute.list_instances, compartment_id=COMPARTMENT_ID).data:
            if inst.lifecycle_state in DEAD_STATES:
                lines.append("| {} | {} | - | - | {} | - |".format(
                    inst.display_name or "-", inst.shape, inst.lifecycle_state))
                continue
            ip = get_instance_public_ip(compute, inst.id) or "-"
            oc = inst.shape_config.ocpus if inst.shape_config else "-"
            mem = inst.shape_config.memory_in_gbs if inst.shape_config else "-"
            lines.append("| {} | {} | {} | {} | {} | {} |".format(
                inst.display_name or "-", inst.shape, oc, mem, inst.lifecycle_state, ip))
    except Exception as e:
        lines.append("| 查询失败 | {} |".format(str(e)[:80]))
    return "\n".join(lines)


def run_summary():
    cap_label = "无（账户免A1额度为0）" if CAPPED_TIER < 0 else TIERS[CAPPED_TIER]["label"]
    return ("本次 run：{} 轮 | 新建 {} / 升配 {} | 成功 {} | 容量不足 {} | 限流 {} | 其他 {}\n"
            "- 当前轮间隔：{}s（自适应，区间 {}~{}s）\n"
            "- 账户 A1 免费档封顶：{}（撞 limit 自动下调）").format(
        RUN["rounds"], RUN["create_try"], RUN["resize_try"], RUN["success"],
        RUN["capacity"], RUN["rate_limit"], RUN["error"],
        int(GAP_CREATE), GAP_MIN_CREATE, GAP_MAX_CREATE, cap_label)


# ---------------- 单轮 ----------------
def do_round(compute, arms):
    import oci
    global PROBE_TIER_IDX
    cur = get_current_tier(arms)
    idx = cur[0] if cur else -1

    # A. 无实例 -> 抢保底（2026-11-02：档位轮换探测，不再固定只试 1核1G）
    if cur is None:
        if CAPPED_TIER < 0:
            print("[ROUND] 账户免A1额度为0（已封顶），跳过创建，仅巡检")
            return arms, None, None
        # 轮换档位：1核1G → 1核6G → 2核12G（见 PROBE_TIERS 注释）。
        # 只挑封顶额度以内的档位；超出封顶的档位跳过（额度到不了，试了只是白撞 limit）。
        usable = [i for i in PROBE_TIERS if i <= CAPPED_TIER]
        if not usable:
            print("[ROUND] 所有档位都超出封顶额度，仅巡检")
            return arms, None, None
        ti = usable[PROBE_TIER_IDX % len(usable)]
        PROBE_TIER_IDX = (PROBE_TIER_IDX + 1) % len(usable)
        _save_probe_tier(PROBE_TIER_IDX)
        t = TIERS[ti]
        print("[ROUND] 无 ARM，探测档位 {}（AD={}，轮换 {}/{}）".format(
            t["label"], current_ad(), usable.index(ti) + 1, len(usable)))
        inst_id, name, fail = action_create(compute, t, dry_run=DRY_RUN)
        if inst_id is None:
            ftype = fail.get("type", "error") if fail else "error"
            if ftype == "capacity":
                RUN["capacity"] += 1
                rotate_ad()                      # 这个 AD 没货，下一轮换 AD 再试
            elif ftype == "limit":
                # 连最小的 1C/1G 都报 limit，先别急着"永久封顶"：紧跟 429 重试之后的 limit 常是瞬时误报
                # （2026-10-27 就因此把整条链钉死在"零额度"，实际账号还有 2核/12G）。
                # 用 Limits API 实测剩余配额：还有额度就只当本轮没抢到，确认为 0 才封顶。
                q = probe_a1_availability()
                if q is not None and (q[0] or 0) >= 1 and (q[1] is None or (q[1] or 0) >= 1):
                    RUN["capacity"] += 1
                    print("[ROUND] 报 limit 但实测仍有额度 {}核/{}G → 判瞬时误报，不封顶".format(q[0], q[1]))
                else:
                    _save_cap(-1)
                    print("[ROUND] 账户免A1额度为0，已封顶（{}h 后自动实测复核）".format(CAP_REPROBE_HOURS))
            elif ftype == "network":
                RUN["network"] += 1
            elif ftype not in ("rate_limit",):
                RUN["error"] += 1
            print("[ROUND] 抢保底失败({}): {}".format(
                ftype, str((fail or {}).get("message"))[:160]))
            return arms, None, fail
        if DRY_RUN:
            state, ip = "RUNNING", "203.0.113.10"
        else:
            state, ip = wait_for_state(compute, inst_id, "RUNNING", WAIT_RUNNING_TIMEOUT)
            ip = get_instance_public_ip(compute, inst_id) if state == "RUNNING" else None
            print("[CREATE] 状态={} IP={}".format(state, ip))
        if state != "RUNNING":
            RUN["error"] += 1
            return arms, None, {"type": "error", "message": "创建后状态 {}".format(state)}
        RUN["success"] += 1
        ev = ("甲骨文抢机：抢到保底 {}".format(t["label"]),
              "## 抢到 {}\n\n- 公网IP: `{}`\n- SSH: `ssh ubuntu@{}`\n- 额度：{}\n\n{}\n\n"
              "之后会自动原地升配到 2核12G，无需重建。".format(
                  t["label"], ip, ip, quota_text([t]), build_instance_table(compute)))
        return list_arm_instances(compute), ev, None

    # B. 已达封顶档位 -> 巡检（封顶可能是账户 free 配额上限，未必是 4C/24G）
    if idx >= CAPPED_TIER:
        cap_label = TIERS[CAPPED_TIER]["label"] if CAPPED_TIER >= 0 else "无（账户免A1额度为0）"
        print("[ROUND] 已达封顶档位 {}，巡检".format(cap_label))
        return arms, None, None

    # C. 原地升配
    inst = cur[1]
    target = TIERS[idx + 1]
    print("[ROUND] {} -> 原地升配 {}".format(TIERS[idx]["label"], target["label"]))
    ok, fail = action_resize_online(compute, inst["id"], target, dry_run=DRY_RUN)
    if not ok and fail and fail.get("type") == "need_stop":
        ok, fail = action_resize_via_stop(compute, inst["id"], target)

    if ok:
        if not DRY_RUN:
            ok2, state = wait_for_state(compute, inst["id"], "RUNNING", 420)
            if not ok2:
                RUN["error"] += 1
                return list_arm_instances(compute), None, {
                    "type": "error", "message": "改配后未回 RUNNING({})".format(state)}
        RUN["success"] += 1
        ip = "-" if DRY_RUN else (get_instance_public_ip(compute, inst["id"]) or "-")
        ev = ("甲骨文抢机：原地升配到 {}（无需重建）".format(target["label"]),
              "## 升配成功：{} -> {}\n\n- 实例：{}\n- 公网IP：`{}`（不变）\n- 额度：{}\n\n{}".format(
                  TIERS[idx]["label"], target["label"], inst["display_name"], ip,
                  quota_text([target]), build_instance_table(compute)))
        return list_arm_instances(compute), ev, None

    ftype = fail.get("type", "error") if fail else "error"
    if ftype == "capacity":
        RUN["capacity"] += 1
        print("[ROUND] 容量不足，继续蹲守")
    elif ftype == "limit":
        # 升配到 target 超出 free 配额（最常见：2026-06-15 政策后纯免费档封顶 2C/12G）。
        # 封顶到当前档 idx，后续只蹲守此档，不再越级白耗尝试。
        _save_cap(idx)
        print("[ROUND] 升配超出免费额度，封顶到 {}，后续只蹲守此档".format(
            TIERS[idx]["label"] if idx >= 0 else "无"))
    elif ftype == "rate_limit":
        print("[ROUND] 限流")
    elif ftype == "network":
        RUN["network"] += 1
    else:
        RUN["error"] += 1
        print("[ROUND] 升配失败({}): {}".format(ftype, str(fail.get("message"))[:120]))
    return arms, None, fail


# ---------------- 主流程 ----------------
def main():
    if "--rounds" in sys.argv:
        global ROUNDS
        ROUNDS = int(sys.argv[sys.argv.index("--rounds") + 1])
    if "--max-seconds" in sys.argv:
        global MAX_SECONDS
        MAX_SECONDS = int(sys.argv[sys.argv.index("--max-seconds") + 1])

    global GAP_CREATE
    GAP_CREATE = _load_gap()
    _load_cap()                              # 读取账户免费 A1 档位封顶（跨日保留）
    global PROBE_TIER_IDX
    PROBE_TIER_IDX = _load_probe_tier()      # 读取档位轮换游标（跨棒保留）
    print("=" * 60)
    print("甲骨文抢机 Actions 版启动 | 北京时间 {} | rounds={} dry={} status={}".format(
        now_bj(), ROUNDS, DRY_RUN, STATUS_ONLY))
    print("[GAP] 自适应轮间隔起始 {}s（区间 {}~{}s）".format(
        int(GAP_CREATE), GAP_MIN_CREATE, GAP_MAX_CREATE))
    cap_label = "无（账户免A1额度为0）" if CAPPED_TIER < 0 else TIERS[CAPPED_TIER]["label"]
    print("[CAP] 账户 A1 免费档封顶：{}（撞 limit 自动下调，跨日保留）".format(cap_label))
    print("[TIER] 探测档位轮换：{}（起始序号 {}）".format(
        " → ".join(TIERS[i]["label"] for i in PROBE_TIERS), PROBE_TIER_IDX))
    print("=" * 60)

    import oci
    try:
        compute = init_clients()
        if maybe_unbrick_cap():
            print("[CAP] 复核后封顶：{}".format(
                "无（账户免A1额度为0）" if CAPPED_TIER < 0 else TIERS[CAPPED_TIER]["label"]))
        refresh_free_quota()              # 实测免费额度（原来硬编码 4C/24G 是错的）
        arms = list_arm_instances(compute)
        cur = get_current_tier(arms)
        print("[状态] ARM {} 台 | 档位 {} | 额度 {}".format(
            len(arms), TIERS[cur[0]]["label"] if cur else "无", quota_text(arms)))

        if STATUS_ONLY:
            push("甲骨文抢机：云端状态",
                 "## 云端账号现状\n\n- ARM：{} 台，档位 {}\n- 额度：{}\n- {}\n\n{}".format(
                     len(arms), TIERS[cur[0]]["label"] if cur else "无（尚未抢到）",
                     quota_text(arms), daily_summary(), build_instance_table(compute)))
            return 0

        state = load_state()
        events_sent = False
        for r in range(1, ROUNDS + 1):
            RUN["rounds"] = r
            print("\n----- 轮 {}/{} -----".format(r, ROUNDS))
            # 每轮重置 429 命中标志（由 call_with_rate_limit 在轮内设置）
            global ROUND_HIT_RATE_LIMIT
            ROUND_HIT_RATE_LIMIT = False
            try:
                arms, event, fail = do_round(compute, arms)
            except Exception as e:
                print("[ROUND ERROR]", str(e)[:200])
                RUN["error"] += 1
                try:
                    arms = list_arm_instances(compute)
                except Exception:
                    pass
                event, fail = None, None

            # 自适应节奏：用轮级标志传真实 429 命中（2026-11-02 修复，见 GAP 段 bug1）
            if not DRY_RUN:
                adapt_gap(bool(ROUND_HIT_RATE_LIMIT) or bool(fail and fail.get("type") == "rate_limit"),
                          retry_after=LAST_RETRY_AFTER)

            if event and not DRY_RUN:
                push(event[0], event[1] + "\n\n---\n" + run_summary() + "\n\n" + daily_summary())
                events_sent = True
            if fail and fail.get("type") == "config":
                push("甲骨文抢机：配置错误需人工处理",
                     "```\n{}\n```".format(str(fail.get("message"))[:800]))
                break

            # 接力模式：跑满本棒时限就优雅收尾，剩余时间交给下一棒
            if MAX_SECONDS and (time.time() - START_TS) >= MAX_SECONDS:
                print("[RELAY] 本棒已跑 {}s（上限 {}s），优雅退出，交给下一棒".format(
                    int(time.time() - START_TS), MAX_SECONDS))
                break

            # 日报：北京时间 02:00。长棒模式下必须放在循环内判断，否则整棒跨过 2 点会漏发
            if not DRY_RUN and REPORT_HOUR_BJ >= 0:
                _n = datetime.datetime.now(TZ_BJ)
                if _n.hour == REPORT_HOUR_BJ and REPORT_SENT_DATE != _n.strftime("%Y-%m-%d"):
                    globals()["REPORT_SENT_DATE"] = _n.strftime("%Y-%m-%d")
                    c3 = get_current_tier(arms)
                    push("甲骨文抢机日报：{}".format(TIERS[c3[0]]["label"] if c3 else "尚未抢到"),
                         "## 日报（北京 {}）\n\n- 档位：{}\n- 额度：{}\n- {}\n- {}\n\n{}\n\n{}".format(
                             now_bj(), TIERS[c3[0]]["label"] if c3 else "无（仍在抢）",
                             quota_text(arms), run_summary(), daily_summary(),
                             build_instance_table(compute), chain_health()))

            if r < ROUNDS:
                c = get_current_tier(arms)
                gap = GAP_CREATE if c is None else (
                    ROUND_GAP_IDLE if c[0] == len(TIERS) - 1 else ROUND_GAP_RESIZE)
                gap = jitter(gap)
                print("[WAIT] {}s".format(gap))
                time.sleep(gap)

        save_state(state)

        # 日报：北京时间 02:00 所在 run（循环里已发过则跳过，避免重复推送）
        _n2 = datetime.datetime.now(TZ_BJ)
        if REPORT_HOUR_BJ >= 0 and _n2.hour == REPORT_HOUR_BJ and not DRY_RUN and REPORT_SENT_DATE != _n2.strftime("%Y-%m-%d"):
            cur2 = get_current_tier(arms)
            push("甲骨文抢机日报：{}".format(TIERS[cur2[0]]["label"] if cur2 else "尚未抢到"),
                 "## 日报（北京 {}）\n\n- 档位：{}\n- 额度：{}\n- {}\n- {}\n\n{}\n\n{}".format(
                     now_bj(), TIERS[cur2[0]]["label"] if cur2 else "无（仍在抢）",
                     quota_text(arms), run_summary(), daily_summary(),
                     build_instance_table(compute), chain_health()))

        print("\n===== 汇总 =====")
        print(run_summary())
        print(daily_summary())
        if DRY_RUN:
            print("===== DRY-RUN 完成，未创建/改配/推送 =====")
        return 0

    except Exception as exc:
        err = traceback.format_exc()
        print("===== 异常 =====")
        print(err)
        if not DRY_RUN:
            try:
                push("甲骨文抢机：云端运行异常", "```\n{}\n```".format(err[:1500]))
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
