# -*- coding: utf-8 -*-
"""
甲骨文抢机脚本 v3（GitHub Actions 版 · 全天多轮 + 原地升配）
==================================================================
与本地版的分工：
    本脚本在 GitHub Actions 云端运行，电脑关机/卸载 WorkBuddy 均不影响。
    由 workflow 每小时拉起一次，每次 run 内做 N 轮尝试后退出，
    下一整点由 cron 再次拉起 —— 全天不间断，且永远不超出私有仓库分钟额度。

核心策略（与本地 v3 一致）：
    1. 无 ARM 实例 -> 抢保底 1核6G（新建）
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

FREE_TOTAL_OCPUS = 4
FREE_TOTAL_MEM_GB = 24

TIERS = [
    {"ocpus": 1, "memory_gb": 6,  "label": "1核6G"},
    {"ocpus": 2, "memory_gb": 12, "label": "2核12G"},
    {"ocpus": 4, "memory_gb": 24, "label": "4核24G"},
]
ARM_SHAPE = "VM.Standard.A1.Flex"

ROUNDS = 3                       # 单次 run 内尝试轮数（workflow 可通过输入覆盖）
ROUND_GAP_CREATE = 75            # 无实例时轮间隔
ROUND_GAP_RESIZE = 150           # 有实例时轮间隔
ROUND_GAP_IDLE = 420             # 顶配巡检间隔

# 接力模式：单棒最长运行秒数（0=不限）。GitHub 单个 job 上限 6 小时，
# workflow 传 19500（约 5.4 小时）跑满后优雅退场，再由 workflow 触发下一棒，
# 从而绕开 GitHub 那个不投递的 cron 调度器。
MAX_SECONDS = 0
START_TS = time.time()
REPORT_SENT_DATE = ""            # 长棒模式下防止日报重复发
POLL_INTERVAL = 15
WAIT_RUNNING_TIMEOUT = 420       # Actions 里单 run 有时长预算，等 RUNNING 上限 7 分钟
STATE_WAIT_TIMEOUT = 240
REPORT_HOUR_BJ = 2               # 北京时间每天 02:00 发日报（老板定的体检推送时间点）
STATE_DIR = os.environ.get("GRAB_STATE_DIR", ".grab_state")   # 跨 run 状态（cache 挂载点）

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


def rate_limit_wait(err, attempt):
    # Actions 单 run 有 12 分钟预算，退避上限收紧到 120s，总重试不超过 ~6 分钟
    wait = get_retry_after(err)
    if wait is None:
        wait = min(30 * (2 ** attempt), 120)
    wait = min(int(wait), 120)
    RUN["rate_limit"] += 1
    print("[RATE-LIMIT] 等待 {}s".format(wait))
    time.sleep(wait)
    return wait


def call_with_rate_limit(func, *args, max_attempts=3, **kwargs):
    attempt = 0
    while True:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if is_rate_limited(e) and attempt < max_attempts:
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
        if e.status == 429 or "toomany" in (e.code or "").lower() or "throttle" in (e.code or "").lower():
            return "rate_limit", msg
        if any(k in low for k in CAPACITY_KEYWORDS):
            return "capacity", msg
        if any(k in low for k in NEED_STOP_KEYWORDS):
            return "need_stop", msg
        if "quota" in low or "service limit" in low:
            return "quota", msg
        if e.status in (401, 404):
            return "config", msg
        return "error", msg
    if is_network_error(e):
        return "network", str(e)
    return "error", str(e)


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


# ---------------- OCI 初始化 ----------------
def init_clients():
    import oci
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
    compute = oci.core.ComputeClient(cfg)
    identity = oci.identity.IdentityClient(cfg)

    # 自动探测 AD 与最新 Ubuntu 24.04 ARM 镜像（失败回退硬编码）
    global AVAILABILITY_DOMAIN, ARM_IMAGE_ID
    try:
        names = [a.name for a in call_with_net_retry(
            identity.list_availability_domains, compartment_id=COMPARTMENT_ID).data]
        if names:
            AVAILABILITY_DOMAIN = names[0]
            print("[AD]", AVAILABILITY_DOMAIN)
    except Exception as e:
        print("[AD] 探测失败，用回退值:", str(e)[:100])
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
    best = max(arms, key=lambda x: x["ocpus"])
    for i, t in enumerate(TIERS):
        if abs(best["ocpus"] - t["ocpus"]) < 0.5:
            return i, best
    return None


def quota_text(arms):
    return "{}C/{}G（免费额度 {}C/{}G）".format(
        sum(a["ocpus"] for a in arms), sum(a["memory_gb"] for a in arms),
        FREE_TOTAL_OCPUS, FREE_TOTAL_MEM_GB)


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
        availability_domain=AVAILABILITY_DOMAIN,
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
    return ("本次 run：{} 轮 | 新建 {} / 升配 {} | 成功 {} | 容量不足 {} | 限流 {} | 其他 {}").format(
        RUN["rounds"], RUN["create_try"], RUN["resize_try"], RUN["success"],
        RUN["capacity"], RUN["rate_limit"], RUN["error"])


# ---------------- 单轮 ----------------
def do_round(compute, arms):
    import oci
    cur = get_current_tier(arms)
    idx = cur[0] if cur else -1

    # A. 无实例 -> 抢保底
    if cur is None:
        t = TIERS[0]
        print("[ROUND] 无 ARM，抢保底 {}".format(t["label"]))
        inst_id, name, fail = action_create(compute, t, dry_run=DRY_RUN)
        if inst_id is None:
            ftype = fail.get("type", "error") if fail else "error"
            if ftype == "capacity":
                RUN["capacity"] += 1
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

    # B. 顶配 -> 巡检
    if idx == len(TIERS) - 1:
        print("[ROUND] 已达顶配")
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

    print("=" * 60)
    print("甲骨文抢机 Actions 版启动 | 北京时间 {} | rounds={} dry={} status={}".format(
        now_bj(), ROUNDS, DRY_RUN, STATUS_ONLY))
    print("=" * 60)

    import oci
    try:
        compute = init_clients()
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
            if not DRY_RUN:
                _n = datetime.datetime.now(TZ_BJ)
                if _n.hour == REPORT_HOUR_BJ and REPORT_SENT_DATE != _n.strftime("%Y-%m-%d"):
                    globals()["REPORT_SENT_DATE"] = _n.strftime("%Y-%m-%d")
                    c3 = get_current_tier(arms)
                    push("甲骨文抢机日报：{}".format(TIERS[c3[0]]["label"] if c3 else "尚未抢到"),
                         "## 日报（北京 {}）\n\n- 档位：{}\n- 额度：{}\n- {}\n- {}\n\n{}".format(
                             now_bj(), TIERS[c3[0]]["label"] if c3 else "无（仍在抢）",
                             quota_text(arms), run_summary(), daily_summary(),
                             build_instance_table(compute)))

            if r < ROUNDS:
                c = get_current_tier(arms)
                gap = ROUND_GAP_CREATE if c is None else (
                    ROUND_GAP_IDLE if c[0] == len(TIERS) - 1 else ROUND_GAP_RESIZE)
                gap = jitter(gap)
                print("[WAIT] {}s".format(gap))
                time.sleep(gap)

        save_state(state)

        # 日报：北京时间 02:00 所在 run（循环里已发过则跳过，避免重复推送）
        _n2 = datetime.datetime.now(TZ_BJ)
        if _n2.hour == REPORT_HOUR_BJ and not DRY_RUN and REPORT_SENT_DATE != _n2.strftime("%Y-%m-%d"):
            cur2 = get_current_tier(arms)
            push("甲骨文抢机日报：{}".format(TIERS[cur2[0]]["label"] if cur2 else "尚未抢到"),
                 "## 日报（北京 {}）\n\n- 档位：{}\n- 额度：{}\n- {}\n- {}\n\n{}".format(
                     now_bj(), TIERS[cur2[0]]["label"] if cur2 else "无（仍在抢）",
                     quota_text(arms), run_summary(), daily_summary(),
                     build_instance_table(compute)))

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
