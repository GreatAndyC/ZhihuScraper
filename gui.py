#!/usr/bin/env python3
"""
知乎爬虫 Web GUI
运行后打开 http://localhost:8080
"""
import threading
import time
import logging
import json
import os
import shutil
import socket
import sys
import queue
import mimetypes
import re
import requests
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import OUTPUT_DIR
from export_utils import (
    build_question_export_meta,
    build_user_export_meta,
    estimate_task_seconds,
    format_datetime_text,
    format_duration,
)
from input_normalizer import normalize_question_input, normalize_user_input
from system_actions import perform_post_task_action
from storage import (
    find_existing_html_for_json,
    find_existing_question_json,
    find_existing_user_json,
    load_question,
    load_user,
)
from scraper.base import USER_AGENTS

_log_history = deque(maxlen=500)
_log_subscribers = []
_log_lock = threading.Lock()
_server_instance = None
_pause_event = threading.Event()
_running_task = False
_current_task_name = ""
_current_task_arg = ""
_stop_event = threading.Event()
_task_state_lock = threading.Lock()
_tasks: list[dict] = []
_task_queue_lock = threading.Lock()
_task_seq = 0
_worker_thread = None
_current_task_id = None
_log_file_path = ""


def _log_dir() -> str:
    path = os.path.join(OUTPUT_DIR, "logs")
    os.makedirs(path, exist_ok=True)
    return path


def _ensure_log_file() -> str:
    global _log_file_path
    if not _log_file_path:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        _log_file_path = os.path.join(_log_dir(), f"gui-{timestamp}.log")
    return _log_file_path


def add_log(msg):
    timestamp = time.strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    with _log_lock:
        _log_history.append(line)
        dead = []
        for subscriber in _log_subscribers:
            try:
                subscriber.put_nowait(line)
            except Exception:
                dead.append(subscriber)
        for subscriber in dead:
            try:
                _log_subscribers.remove(subscriber)
            except ValueError:
                pass
    try:
        with open(_ensure_log_file(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    _update_running_task_progress_from_log(line)


def _remove_log_subscriber(subscriber):
    with _log_lock:
        try:
            _log_subscribers.remove(subscriber)
        except ValueError:
            pass

def _snapshot_task_state():
    with _task_state_lock:
        return _running_task, _current_task_name, _current_task_arg


def _set_task_state(running, task_name="", task_arg=""):
    global _running_task, _current_task_name, _current_task_arg
    with _task_state_lock:
        _running_task = running
        _current_task_name = task_name
        _current_task_arg = task_arg


def _split_targets(raw: str) -> list[str]:
    items = []
    for part in (raw or "").replace("\r", "\n").split("\n"):
        text = part.strip()
        if text:
            items.append(text)
    return items


def _resolve_initial_task_label(cmd: str, normalized_arg: str, raw_arg: str) -> str:
    fallback = raw_arg or normalized_arg or cmd
    try:
        if cmd == "question":
            existing_json = find_existing_question_json(normalized_arg)
            if existing_json:
                question = load_question(existing_json)
                return question.title or fallback
            return _resolve_question_title(normalized_arg) or fallback
        if cmd == "user":
            existing_json = find_existing_user_json(normalized_arg)
            if existing_json:
                user = load_user(existing_json)
                return user.name or fallback
            return _resolve_user_name(normalized_arg) or fallback
    except Exception:
        return fallback
    return fallback


def _resolver_headers() -> dict:
    headers = {
        "User-Agent": USER_AGENTS[0],
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.zhihu.com/",
        "Origin": "https://www.zhihu.com",
    }
    cookie = os.getenv("ZHIHU_COOKIE", "").strip()
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _resolve_question_title(question_id: str) -> str:
    response = requests.get(
        f"https://www.zhihu.com/api/v4/questions/{question_id}",
        params={"include": "title"},
        headers=_resolver_headers(),
        timeout=8,
    )
    if response.status_code == 200:
        data = response.json() or {}
        return (data.get("title") or "").strip()
    return ""


def _resolve_user_name(user_id: str) -> str:
    response = requests.get(
        f"https://www.zhihu.com/api/v4/members/{user_id}",
        params={"include": "name"},
        headers=_resolver_headers(),
        timeout=8,
    )
    if response.status_code == 200:
        data = response.json() or {}
        return (data.get("name") or "").strip()
    return ""


def _next_task_id() -> int:
    global _task_seq
    with _task_queue_lock:
        _task_seq += 1
        return _task_seq


def _create_task(cmd: str, raw_arg: str, mode: str, raw_types: str, profile: str, html_variant: str, post_action: str, force: bool) -> dict:
    content_types = [item.strip() for item in raw_types.split(",") if item.strip()]
    normalized_arg = raw_arg
    if cmd == "question":
        normalized_arg = normalize_question_input(raw_arg)
    elif cmd == "user":
        normalized_arg = normalize_user_input(raw_arg)
    label = _resolve_initial_task_label(cmd, normalized_arg, raw_arg)
    estimated_seconds = estimate_task_seconds(cmd, mode=mode, content_types=content_types, html_variant=html_variant)
    return {
        "id": _next_task_id(),
        "cmd": cmd,
        "label": label,
        "raw_arg": raw_arg,
        "arg": normalized_arg,
        "mode": mode,
        "raw_types": raw_types,
        "content_types": content_types,
        "profile": profile,
        "html_variant": html_variant,
        "post_action": post_action or "none",
        "force": force,
        "status": "pending",
        "submitted_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "estimated_seconds": estimated_seconds,
        "error": "",
    }


def _serialize_task(task: dict) -> dict:
    estimated_start = task.get("estimated_start")
    estimated_finish = task.get("estimated_finish")
    started_at = task.get("started_at")
    finished_at = task.get("finished_at")
    actual_duration_seconds = 0
    if started_at and finished_at:
        actual_duration_seconds = max(1, finished_at - started_at)
    elif started_at and task.get("status") == "running":
        actual_duration_seconds = max(1, time.time() - started_at)
    return {
        "id": task["id"],
        "cmd": task["cmd"],
        "arg": task["arg"],
        "raw_arg": task["raw_arg"],
        "label": task.get("label", task["raw_arg"] or task["arg"]),
        "mode": task["mode"],
        "profile": task["profile"],
        "html_variant": task["html_variant"],
        "force": task.get("force", False),
        "content_types": task.get("content_types", []),
        "status": task["status"],
        "submitted_at": task.get("submitted_at"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "submitted_text": format_datetime_text(task.get("submitted_at")),
        "started_text": format_datetime_text(task.get("started_at")),
        "finished_text": format_datetime_text(task.get("finished_at")),
        "estimated_seconds": task.get("estimated_seconds", 0),
        "estimated_duration_text": format_duration(task.get("estimated_seconds", 0)),
        "actual_duration_seconds": actual_duration_seconds,
        "actual_duration_text": format_duration(actual_duration_seconds) if actual_duration_seconds else "",
        "latest_stage": task.get("latest_stage", ""),
        "latest_progress_text": task.get("latest_progress_text", ""),
        "latest_eta_text": task.get("latest_eta_text", ""),
        "latest_elapsed_text": task.get("latest_elapsed_text", ""),
        "estimated_finish": estimated_finish,
        "estimated_start": estimated_start,
        "estimated_start_text": format_datetime_text(estimated_start),
        "estimated_finish_text": format_datetime_text(estimated_finish),
        "error": task.get("error", ""),
        "can_delete": task.get("can_delete", False),
        "can_move_up": task.get("can_move_up", False),
        "can_move_down": task.get("can_move_down", False),
    }


def _queue_snapshot() -> list[dict]:
    with _task_queue_lock:
        tasks = [dict(task) for task in _tasks[-60:]]

    cursor = time.time()
    for task in tasks:
        status = task.get("status")
        estimated_seconds = max(1, int(task.get("estimated_seconds") or 1))
        if status == "running":
            started_at = task.get("started_at") or cursor
            task["estimated_start"] = started_at
            task["estimated_finish"] = started_at + estimated_seconds
            cursor = task["estimated_finish"]
        elif status == "pending":
            task["estimated_start"] = cursor
            task["estimated_finish"] = cursor + estimated_seconds
            cursor = task["estimated_finish"]
        else:
            task["estimated_start"] = task.get("started_at")
            task["estimated_finish"] = task.get("finished_at")

    pending_positions = [idx for idx, task in enumerate(tasks) if task.get("status") == "pending"]
    for idx, task in enumerate(tasks):
        task["can_delete"] = task.get("status") != "running"
        if task.get("status") == "pending":
            pos = pending_positions.index(idx)
            task["can_move_up"] = pos > 0
            task["can_move_down"] = pos < len(pending_positions) - 1
        else:
            task["can_move_up"] = False
            task["can_move_down"] = False
    return [_serialize_task(task) for task in tasks]


def _task_signature(task: dict) -> tuple:
    return (
        task["cmd"],
        task["arg"],
        task.get("mode"),
        task.get("raw_types", ""),
        task.get("profile"),
        task.get("html_variant"),
        bool(task.get("force")),
    )


def _enqueue_tasks(cmd: str, raw_args: list[str], mode: str, raw_types: str, profile: str, html_variant: str, post_action: str, force: bool) -> list[dict]:
    new_tasks = []
    for raw_arg in raw_args:
        task = _create_task(cmd, raw_arg, mode, raw_types, profile, html_variant, post_action, force)
        new_tasks.append(task)
    with _task_queue_lock:
        existing_signatures = {
            _task_signature(task)
            for task in _tasks
            if task["status"] in {"pending", "running"}
        }
        accepted = []
        for task in new_tasks:
            signature = _task_signature(task)
            if signature in existing_signatures:
                add_log(f"⚠ 已有相同任务正在排队或执行，已跳过重复提交: {task['cmd']} {task['arg']}")
                continue
            existing_signatures.add(signature)
            accepted.append(task)
        _tasks.extend(accepted)
    return accepted


def _pending_task_exists() -> bool:
    with _task_queue_lock:
        return any(task["status"] == "pending" for task in _tasks)


def _pick_next_pending_task() -> dict | None:
    global _current_task_id
    with _task_queue_lock:
        for task in _tasks:
            if task["status"] == "pending":
                task["status"] = "running"
                task["started_at"] = time.time()
                _current_task_id = task["id"]
                return task
    return None


def _finish_task(task: dict, status: str, error: str = "") -> bool:
    global _current_task_id
    trigger_post_action = False
    post_action = "none"
    with _task_queue_lock:
        task["status"] = status
        task["finished_at"] = time.time()
        task["error"] = error
        _current_task_id = None
        pending_exists = any(item["status"] == "pending" for item in _tasks)
        running_exists = any(item["status"] == "running" for item in _tasks)
        if not pending_exists and not running_exists:
            trigger_post_action = True
            post_action = task.get("post_action") or "none"
    if trigger_post_action and post_action != "none":
        ok, message = perform_post_task_action(post_action)
        add_log(("✓ " if ok else "⚠ ") + message)
    return trigger_post_action


def _update_task_label(task: dict, label: str) -> None:
    text = (label or "").strip()
    if text:
        with _task_queue_lock:
            task["label"] = text


def _delete_task(task_id: int) -> tuple[bool, str]:
    with _task_queue_lock:
        for idx, task in enumerate(_tasks):
            if task["id"] != task_id:
                continue
            if task["status"] == "running":
                return False, "运行中的任务不能删除"
            label = task.get("label") or task.get("arg") or task.get("raw_arg") or task.get("cmd")
            _tasks.pop(idx)
            return True, f"已删除队列任务 #{task_id}: {label}"
    return False, f"未找到任务 #{task_id}"


def _move_pending_task(task_id: int, direction: str) -> tuple[bool, str]:
    with _task_queue_lock:
        pending_indices = [idx for idx, task in enumerate(_tasks) if task["status"] == "pending"]
        if not pending_indices:
            return False, "当前没有可调整顺序的排队任务"

        target_pos = None
        for pos, idx in enumerate(pending_indices):
            if _tasks[idx]["id"] == task_id:
                target_pos = pos
                break
        if target_pos is None:
            return False, f"任务 #{task_id} 当前不可调整顺序"

        if direction == "up":
            if target_pos == 0:
                return False, f"任务 #{task_id} 已经在排队列表最前面"
            swap_a = pending_indices[target_pos]
            swap_b = pending_indices[target_pos - 1]
        elif direction == "down":
            if target_pos >= len(pending_indices) - 1:
                return False, f"任务 #{task_id} 已经在排队列表最后面"
            swap_a = pending_indices[target_pos]
            swap_b = pending_indices[target_pos + 1]
        else:
            return False, f"未知移动方向: {direction}"

        label = _tasks[swap_a].get("label") or _tasks[swap_a].get("arg") or _tasks[swap_a].get("cmd")
        _tasks[swap_a], _tasks[swap_b] = _tasks[swap_b], _tasks[swap_a]
    return True, f"已将任务 #{task_id} ({label}) {'上移' if direction == 'up' else '下移'}"


def _mode_label(mode: str) -> str:
    return {
        "full": "完整内容（HTML/图片）",
        "text": "纯文字 JSON",
        "fast": "快速预览",
    }.get(mode, mode)


def _profile_label(profile: str) -> str:
    return {
        "standard": "标准模式",
        "conservative": "保守模式（更慢、更稳）",
    }.get(profile, profile)


def _html_variant_label(variant: str) -> str:
    return {
        "dir": "目录模式（HTML + assets）",
        "single": "单文件离线 HTML",
    }.get(variant, variant)


def _post_action_label(action: str) -> str:
    return {
        "none": "无",
        "display_off": "熄灭屏幕",
        "sleep": "系统休眠",
    }.get(action or "none", action or "none")


def _update_running_task_progress_from_log(line: str) -> None:
    progress_markers = ("进度", "预计完成", "已用时", "正文补全", "离线资源下载")
    if not any(marker in line for marker in progress_markers):
        return

    stage = ""
    if "] " in line:
        body = line.split("] ", 1)[1]
    else:
        body = line
    if ":" in body:
        stage = body.split(":", 1)[0].strip()
    elif "：" in body:
        stage = body.split("：", 1)[0].strip()

    progress_text = ""
    percent_match = re.search(r"(\d+(?:\.\d+)?)%", body)
    count_match = re.search(r"(\d+/\d+)", body)
    if percent_match and count_match:
        progress_text = f"{count_match.group(1)} ({percent_match.group(1)}%)"
    elif percent_match:
        progress_text = f"{percent_match.group(1)}%"
    elif count_match:
        progress_text = count_match.group(1)

    eta_text = ""
    eta_match = re.search(r"预计完成(?:时间)?[:=]\s*([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}|未知)", body)
    if eta_match:
        eta_text = eta_match.group(1)

    elapsed_text = ""
    elapsed_match = re.search(r"已用时[:=]\s*([^,，]+)", body)
    if elapsed_match:
        elapsed_text = elapsed_match.group(1).strip()

    with _task_queue_lock:
        running_task = next((task for task in _tasks if task["status"] == "running"), None)
        if not running_task:
            return
        if stage:
            running_task["latest_stage"] = stage
        if progress_text:
            running_task["latest_progress_text"] = progress_text
        if eta_text:
            running_task["latest_eta_text"] = eta_text
        if elapsed_text:
            running_task["latest_elapsed_text"] = elapsed_text


def _can_reuse_existing_mode(existing_mode: str, requested_mode: str) -> bool:
    existing_mode = existing_mode or "full"
    if requested_mode == "full":
        return existing_mode == "full"
    if requested_mode in {"text", "fast"}:
        return existing_mode in {"text", "fast", "full"}
    return False


def _can_reuse_user_content_types(existing_types: list[str], requested_types: list[str]) -> bool:
    existing = set(existing_types or [])
    requested = set(requested_types or [])
    return requested.issubset(existing)


def _ensure_worker_running():
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return

    def worker():
        global _worker_thread
        while True:
            task = _pick_next_pending_task()
            if not task:
                _worker_thread = None
                return
            _set_task_state(True, task["cmd"], task["arg"])
            ok, stopped, error = _execute_task(task)
            _set_task_state(False)
            if stopped:
                queue_idle = _finish_task(task, "stopped", error)
            elif ok:
                queue_idle = _finish_task(task, "completed", error)
            else:
                queue_idle = _finish_task(task, "failed", error)
            if queue_idle:
                add_log("空闲中，可继续添加任务")

    _worker_thread = threading.Thread(target=worker, daemon=True)
    _worker_thread.start()


def _execute_task(task: dict) -> tuple[bool, bool, str]:
    _pause_event.clear()
    _stop_event.clear()
    _load_local_env()

    cmd = task["cmd"]
    arg = task["arg"]
    mode = task["mode"]
    raw_types = task.get("raw_types", "")
    profile = task.get("profile", "standard")
    html_variant = task.get("html_variant", "dir")
    force = bool(task.get("force", False))
    conservative_mode = profile == "conservative"
    content_types = [item.strip() for item in raw_types.split(",") if item.strip()]

    try:
        if cmd == "question":
            if task.get("raw_arg") and task["raw_arg"] != arg:
                add_log(f"✓ 已从链接识别问题 ID: {arg}")
            return _scrape_question(
                task=task,
                question_id=arg,
                source_input=task.get("raw_arg", arg),
                content_mode=mode,
                conservative_mode=conservative_mode,
                html_variant=html_variant,
                profile=profile,
                force=force,
            )
        if cmd == "user":
            if task.get("raw_arg") and task["raw_arg"] != arg:
                add_log(f"✓ 已从链接识别用户 ID: {arg}")
            return _scrape_user(
                task=task,
                user_id=arg,
                source_input=task.get("raw_arg", arg),
                content_mode=mode,
                content_types=content_types or ["answer", "article", "pin"],
                conservative_mode=conservative_mode,
                html_variant=html_variant,
                profile=profile,
                force=force,
            )
        if cmd in {"hot-list", "recommend"}:
            return _scrape_feed(cmd)
        return False, False, f"未知任务类型: {cmd}"
    except Exception as exc:
        import traceback

        add_log(f"✗ 错误: {exc}")
        add_log(traceback.format_exc())
        return False, _stop_event.is_set(), str(exc)


def _wait_if_paused():
    while _pause_event.is_set():
        if _stop_event.is_set():
            return False
        time.sleep(0.2)
    return not _stop_event.is_set()


def _should_stop():
    return _stop_event.is_set()


def _load_local_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def _output_web_path(abs_path: str) -> str:
    normalized_output = os.path.abspath(OUTPUT_DIR)
    normalized_target = os.path.abspath(abs_path)
    if normalized_target.startswith(normalized_output):
        relative_path = os.path.relpath(normalized_target, normalized_output).replace(os.sep, "/")
        return f"/output/{relative_path}"
    return f"/file?path={normalized_target}"


def _recent_html_files(limit: int = 10) -> list[dict]:
    items = []
    html_root = os.path.join(OUTPUT_DIR, "html")
    for kind, folder in (("question", "questions"), ("user", "users")):
        base_dir = os.path.join(html_root, folder)
        if not os.path.isdir(base_dir):
            continue
        for name in os.listdir(base_dir):
            if not name.lower().endswith(".html"):
                continue
            path = os.path.join(base_dir, name)
            if not os.path.isfile(path):
                continue
            try:
                modified_at = os.path.getmtime(path)
            except OSError:
                continue
            items.append(
                {
                    "kind": kind,
                    "name": os.path.splitext(name)[0],
                    "path": path,
                    "web_path": _output_web_path(path),
                    "modified_at": modified_at,
                    "modified_text": format_datetime_text(modified_at),
                }
            )
    items.sort(key=lambda item: item["modified_at"], reverse=True)
    return items[:limit]


def _safe_asset_key(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "-", name).strip("-") or "item"


def _delete_recent_html_bundle(abs_path: str) -> tuple[bool, str]:
    html_root = os.path.abspath(os.path.join(OUTPUT_DIR, "html"))
    target = os.path.abspath(abs_path or "")
    if not target.startswith(html_root) or not os.path.isfile(target):
        return False, "未找到可删除的本地 HTML 文件"

    relative = os.path.relpath(target, html_root).replace(os.sep, "/")
    parts = relative.split("/")
    if len(parts) < 2 or parts[0] not in {"questions", "users"}:
        return False, "只允许删除 output/html 下的问题页或用户页"

    group = "questions" if parts[0] == "questions" else "users"
    filename = os.path.basename(target)
    stem = os.path.splitext(filename)[0]
    asset_key = stem[:-7] if stem.endswith("-single") else stem
    asset_dir = os.path.join(html_root, "assets", group, _safe_asset_key(asset_key))

    removed_parts = []
    try:
        os.remove(target)
        removed_parts.append("HTML")
    except OSError as exc:
        return False, f"删除 HTML 失败: {exc}"

    if os.path.isdir(asset_dir):
        try:
            shutil.rmtree(asset_dir)
            removed_parts.append("本地图片资源")
        except OSError as exc:
            return False, f"HTML 已删除，但删除本地图片资源失败: {exc}"

    return True, f"已删除离线浏览页: {filename}（{' + '.join(removed_parts)}）"


class GUIHandler(BaseHTTPRequestHandler):
    def log_message(self, style, msg, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode("utf-8"))

        elif parsed.path == "/logs":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            subscriber = queue.Queue()
            with _log_lock:
                _log_subscribers.append(subscriber)
                history = list(_log_history)
            try:
                for line in history:
                    self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
                self.wfile.flush()
                while True:
                    try:
                        line = subscriber.get(timeout=30)
                        self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                _remove_log_subscriber(subscriber)

        elif parsed.path == "/status":
            running, task_name, task_arg = _snapshot_task_state()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            import sys
            self.wfile.write(json.dumps({
                "running": running,
                "task": task_name,
                "arg": task_arg,
                "paused": _pause_event.is_set(),
                "python": sys.executable[:50],
                "queue": _queue_snapshot(),
                "recent_html": _recent_html_files(),
            }).encode("utf-8"))

        elif parsed.path == "/file":
            qs = parse_qs(parsed.query)
            target = unquote(qs.get("path", [""])[0])
            normalized_output = os.path.abspath(OUTPUT_DIR)
            normalized_target = os.path.abspath(target) if target else ""
            if not normalized_target.startswith(normalized_output) or not os.path.isfile(normalized_target):
                self.send_error(404)
                return
            content_type, _ = mimetypes.guess_type(normalized_target)
            self.send_response(200)
            self.send_header("Content-Type", content_type or "application/octet-stream")
            self.end_headers()
            with open(normalized_target, "rb") as f:
                self.wfile.write(f.read())

        elif parsed.path.startswith("/output/"):
            normalized_output = os.path.abspath(OUTPUT_DIR)
            relative_path = unquote(parsed.path.removeprefix("/output/"))
            normalized_target = os.path.abspath(os.path.join(normalized_output, relative_path))
            if not normalized_target.startswith(normalized_output) or not os.path.isfile(normalized_target):
                self.send_error(404)
                return
            content_type, _ = mimetypes.guess_type(normalized_target)
            self.send_response(200)
            self.send_header("Content-Type", content_type or "application/octet-stream")
            self.end_headers()
            with open(normalized_target, "rb") as f:
                self.wfile.write(f.read())

        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/pause":
            _pause_event.set()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "paused"}).encode("utf-8"))

        elif parsed.path == "/resume":
            _pause_event.clear()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "resumed"}).encode("utf-8"))

        elif parsed.path == "/stop":
            running, _, _ = _snapshot_task_state()
            _stop_event.set()
            if running:
                add_log("⚠ 已收到终止请求，正在安全停止当前任务...")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "stopping" if running else "idle"}).encode("utf-8"))

        elif parsed.path == "/scrape":
            qs = parse_qs(parsed.query)
            cmd = qs.get("cmd", [""])[0]
            arg = qs.get("arg", [""])[0]
            mode = qs.get("mode", ["full"])[0]
            types = qs.get("types", [""])[0]
            profile = qs.get("profile", ["standard"])[0]
            html_variant = qs.get("html_variant", ["dir"])[0]
            post_action = qs.get("post_action", ["none"])[0]
            force = qs.get("force", ["0"])[0] == "1"

            raw_args = [""] if cmd in {"hot-list", "recommend"} else _split_targets(arg)
            if cmd not in {"hot-list", "recommend"} and not raw_args:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "error": "请输入有效的 ID 或链接"}).encode("utf-8"))
                return

            queued = _enqueue_tasks(cmd, raw_args, mode, types, profile, html_variant, post_action, force)
            for task in queued:
                target = task.get("label") or task["raw_arg"] or task["arg"] or task["cmd"]
                add_log(
                    "▶ 已加入队列: "
                    f"#{task['id']} {task['cmd']} {target} "
                    f"[{task['mode']}] [{_profile_label(task['profile'])}] [{_html_variant_label(task['html_variant'])}]"
                    + (" [强制重抓]" if task.get("force") else "")
                )
            _ensure_worker_running()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "queued", "queued": len(queued)}).encode("utf-8"))

        elif parsed.path == "/queue/delete":
            qs = parse_qs(parsed.query)
            try:
                task_id = int(qs.get("id", ["0"])[0])
            except ValueError:
                task_id = 0
            ok, message = _delete_task(task_id)
            if ok:
                add_log(f"✓ {message}")
            else:
                add_log(f"⚠ {message}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": ok, "message": message}).encode("utf-8"))

        elif parsed.path == "/queue/move":
            qs = parse_qs(parsed.query)
            try:
                task_id = int(qs.get("id", ["0"])[0])
            except ValueError:
                task_id = 0
            direction = qs.get("direction", [""])[0]
            ok, message = _move_pending_task(task_id, direction)
            if ok:
                add_log(f"✓ {message}")
            else:
                add_log(f"⚠ {message}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": ok, "message": message}).encode("utf-8"))

        elif parsed.path == "/recent-html/delete":
            qs = parse_qs(parsed.query)
            target = unquote(qs.get("path", [""])[0])
            ok, message = _delete_recent_html_bundle(target)
            if ok:
                add_log(f"✓ {message}")
            else:
                add_log(f"⚠ {message}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": ok, "message": message}).encode("utf-8"))

        else:
            self.send_error(404)


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        exc_type, exc, _ = sys.exc_info()
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def _scrape_question(
    task: dict,
    question_id: str,
    source_input: str,
    content_mode: str,
    conservative_mode: bool,
    html_variant: str,
    profile: str,
    force: bool,
) -> tuple[bool, bool, str]:
    from scraper.question import QuestionScraper
    from storage import prepare_question_batch_dir, save_question_batch, save_question
    from renderers import render_question_html
    import logging

    # 接管 scraper 的日志
    class QueueHandler(logging.Handler):
        def emit(self, record):
            add_log(self.format(record))

    orig_handlers = []
    scraper_logger = logging.getLogger("scraper.question")
    scraper_logger.setLevel(logging.INFO)
    qh = QueueHandler()
    qh.setFormatter(logging.Formatter("%(message)s"))
    orig_handlers = scraper_logger.handlers[:]
    scraper_logger.handlers = [qh]

    add_log(f"▶ 开始执行：爬取问题 {question_id}")
    add_log("✓ 正在连接知乎...")
    add_log(f"✓ 抓取模式: {_mode_label(content_mode)}")
    add_log(f"✓ 抓取节奏: {_profile_label(profile)}")
    add_log(f"✓ HTML 导出: {_html_variant_label(html_variant)}")
    add_log(f"✓ 本地去重: {'强制重抓' if force else '检测到本地已有内容则复用/跳过'}")

    if not force:
        existing_json = find_existing_question_json(question_id)
        if existing_json:
            try:
                existing_question = load_question(existing_json)
                existing_mode = existing_question.content_mode or (existing_question.export_meta or {}).get("content_mode", "full")
                if _can_reuse_existing_mode(existing_mode, content_mode):
                    html_path = find_existing_html_for_json(existing_json, "questions", html_variant)
                    if content_mode == "full" and not html_path:
                        add_log("✓ 检测到本地已有完整 JSON，正在补生成所需 HTML 浏览页...")
                        html_path = render_question_html(
                            existing_question,
                            conservative_mode=conservative_mode,
                            progress_callback=add_log,
                            variant=html_variant,
                        )
                    _update_task_label(task, existing_question.title or question_id)
                    add_log(f"✓ 本地已存在问题归档，跳过重复抓取: {existing_json}")
                    if html_path:
                        add_log(f"✓ 浏览页: {_output_web_path(html_path)}")
                    return True, False, ""
            except Exception as exc:
                add_log(f"⚠ 检查本地问题归档失败，继续抓取: {exc}")

    scraper = None
    try:
        scraper = QuestionScraper(conservative_mode=conservative_mode)
        add_log("✓ 已连接，开始获取问题数据...")
        batch_dir = prepare_question_batch_dir(question_id)
        add_log(f"✓ 分批保存目录: {batch_dir}")

        q = scraper.fetch_all(
            question_id,
            should_stop=_should_stop,
            wait_if_paused=_wait_if_paused,
            batch_callback=save_question_batch,
            content_mode=content_mode,
        )
        if not q:
            if _stop_event.is_set():
                add_log("⚠ 任务已终止")
                add_log("⚠ 已抓取的批次文件已保留，可稍后手动合并")
            else:
                add_log("✗ 获取问题失败，请检查 Cookie 或问题 ID")
            return False, _stop_event.is_set(), "获取问题失败"

        add_log(f"✓ 问题全名：{q.title}")
        _update_task_label(task, q.title or question_id)
        add_log(f"✓ 已抓回答：{len(q.answers)} / 声明总数：{q.answer_count}")
        html_path = render_question_html(
            q,
            conservative_mode=conservative_mode,
            progress_callback=add_log,
            variant=html_variant,
        )
        q.export_meta = build_question_export_meta(
            q,
            crawl_profile=profile,
            html_variant=html_variant,
            source_input=source_input,
            output_html=html_path,
        )
        path = save_question(q, question_id)
        q.export_meta["output_json"] = path
        path = save_question(q, question_id)
        add_log(f"✓ 已保存至: {path}")
        add_log(f"✓ 浏览页: {_output_web_path(html_path)}")
        add_log(f"✓ 浏览页文件: {html_path}")
        add_log(f"✓ 完成！共爬取 {len(q.answers)} 条回答")
        return True, False, ""

    except Exception as e:
        add_log(f"✗ 错误: {e}")
        import traceback
        add_log(traceback.format_exc())
        traceback.print_exc()
        return False, _stop_event.is_set(), str(e)
    finally:
        try:
            if scraper:
                scraper.close()
        except Exception:
            pass
        scraper_logger.handlers = orig_handlers


def _scrape_user(
    task: dict,
    user_id: str,
    source_input: str,
    content_mode: str,
    content_types: list[str],
    conservative_mode: bool,
    html_variant: str,
    profile: str,
    force: bool,
) -> tuple[bool, bool, str]:
    from scraper.user import UserScraper
    from storage import save_user
    from renderers import render_user_html
    import logging

    class QueueHandler(logging.Handler):
        def emit(self, record):
            add_log(self.format(record))

    orig_handlers = []
    scraper_logger = logging.getLogger("scraper.user")
    scraper_logger.setLevel(logging.INFO)
    qh = QueueHandler()
    qh.setFormatter(logging.Formatter("%(message)s"))
    orig_handlers = scraper_logger.handlers[:]
    scraper_logger.handlers = [qh]

    add_log(f"▶ 开始执行：爬取用户 {user_id}")
    add_log("✓ 正在连接知乎...")
    add_log(f"✓ 抓取模式: {_mode_label(content_mode)}")
    add_log(f"✓ 内容类型: {', '.join(content_types)}")
    add_log(f"✓ 抓取节奏: {_profile_label(profile)}")
    add_log(f"✓ HTML 导出: {_html_variant_label(html_variant)}")
    add_log(f"✓ 本地去重: {'强制重抓' if force else '检测到本地已有内容则复用/跳过'}")
    if content_mode == "full":
        add_log("✓ 用户主页抓取分两步：先抓内容列表，再补正文详情；完整模式下可能持续较久")

    if not force:
        existing_json = find_existing_user_json(user_id)
        if existing_json:
            try:
                existing_user = load_user(existing_json)
                existing_mode = existing_user.content_mode or (existing_user.export_meta or {}).get("content_mode", "full")
                if _can_reuse_existing_mode(existing_mode, content_mode) and _can_reuse_user_content_types(existing_user.content_types, content_types):
                    html_path = find_existing_html_for_json(existing_json, "users", html_variant)
                    if content_mode == "full" and not html_path:
                        add_log("✓ 检测到本地已有完整用户 JSON，正在补生成所需 HTML 浏览页...")
                        html_path = render_user_html(
                            existing_user,
                            conservative_mode=conservative_mode,
                            progress_callback=add_log,
                            variant=html_variant,
                        )
                    _update_task_label(task, existing_user.name or user_id)
                    add_log(f"✓ 本地已存在用户归档，跳过重复抓取: {existing_json}")
                    if html_path:
                        add_log(f"✓ 浏览页: {_output_web_path(html_path)}")
                    return True, False, ""
            except Exception as exc:
                add_log(f"⚠ 检查本地用户归档失败，继续抓取: {exc}")

    scraper = None
    try:
        scraper = UserScraper(conservative_mode=conservative_mode)
        add_log("✓ 已连接，开始获取用户数据...")

        u = scraper.fetch_all(
            user_id,
            should_stop=_should_stop,
            wait_if_paused=_wait_if_paused,
            content_mode=content_mode,
            content_types=content_types,
        )
        if not u:
            if _stop_event.is_set():
                add_log("⚠ 任务已终止")
            else:
                add_log("✗ 获取用户失败，请检查 Cookie 或用户 ID")
            return False, _stop_event.is_set(), "获取用户失败"

        add_log(f"✓ 用户名：{u.name}")
        _update_task_label(task, u.name or user_id)
        add_log(f"✓ 已抓内容：{len(u.activities)} 条")
        rich_count = sum(1 for item in u.activities if item.content_html)
        add_log(f"✓ 正文补全：成功 {rich_count} 条，失败 {len(u.activities) - rich_count} 条")
        html_path = render_user_html(
            u,
            conservative_mode=conservative_mode,
            progress_callback=add_log,
            variant=html_variant,
        )
        u.export_meta = build_user_export_meta(
            u,
            crawl_profile=profile,
            html_variant=html_variant,
            source_input=source_input,
            output_html=html_path,
        )
        path = save_user(u, user_id)
        u.export_meta["output_json"] = path
        path = save_user(u, user_id)
        add_log(f"✓ 已保存至: {path}")
        add_log(f"✓ 浏览页: {_output_web_path(html_path)}")
        add_log(f"✓ 浏览页文件: {html_path}")
        add_log(f"✓ 完成！")
        return True, False, ""

    except Exception as e:
        add_log(f"✗ 错误: {e}")
        import traceback
        add_log(traceback.format_exc())
        traceback.print_exc()
        return False, _stop_event.is_set(), str(e)
    finally:
        try:
            if scraper:
                scraper.close()
        except Exception:
            pass
        scraper_logger.handlers = orig_handlers


def _scrape_feed(cmd: str) -> tuple[bool, bool, str]:
    from scraper import FeedScraper

    try:
        add_log("✓ 正在连接知乎...")
        scraper = FeedScraper()
        if cmd == "hot-list":
            items = scraper.fetch_hot_list(limit=30)
            path = "output/hot-list.json"
        else:
            items = scraper.fetch_recommend(page=0, per_page=20)
            path = "output/recommend.json"
        add_log(f"✓ 获取成功，共 {len(items)} 条")
        os.makedirs("output", exist_ok=True)
        import json as j2
        with open(path, "w", encoding="utf-8") as f:
            j2.dump(items, f, ensure_ascii=False, indent=2, default=str)
        add_log(f"✓ 已保存至: {path}")
        add_log("✓ 完成！")
        return True, False, ""
    except Exception as exc:
        add_log(f"✗ 错误: {exc}")
        return False, False, str(exc)


HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>知乎爬虫</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif;
    background: radial-gradient(circle at top, #1a2332 0%, #0d1117 42%, #090c10 100%);
    color: #e8edf4;
    min-height: 100vh;
    padding: 32px 20px 48px;
  }
  a { color: #68d4ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .page { max-width: 1380px; margin: 0 auto; }
  .hero {
    margin-bottom: 22px;
    padding: 26px 28px;
    border-radius: 24px;
    background: linear-gradient(135deg, rgba(18, 26, 38, 0.95), rgba(8, 18, 26, 0.92));
    border: 1px solid rgba(104, 212, 255, 0.14);
    box-shadow: 0 16px 48px rgba(0, 0, 0, 0.28);
  }
  .hero h1 {
    font-size: clamp(30px, 5vw, 42px);
    line-height: 1.1;
    margin-bottom: 8px;
    letter-spacing: -0.03em;
  }
  .hero h1 span {
    background: linear-gradient(135deg, #68d4ff, #6af0c2);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }
  .hero p { color: #8b97a8; font-size: 15px; line-height: 1.75; max-width: 880px; }
  .layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 360px;
    gap: 20px;
    align-items: start;
  }
  .stack { display: flex; flex-direction: column; gap: 20px; }
  .card {
    background: rgba(15, 20, 28, 0.92);
    border: 1px solid rgba(104, 212, 255, 0.1);
    border-radius: 22px;
    padding: 22px;
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.22);
  }
  .card-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    margin-bottom: 18px;
  }
  .card-title {
    font-size: 12px;
    color: #6e7a8b;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    font-weight: 700;
  }
  .tabs { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 18px; }
  .tab {
    border: 1px solid rgba(104, 212, 255, 0.14);
    background: rgba(255, 255, 255, 0.02);
    color: #8fa2bb;
    border-radius: 999px;
    padding: 10px 16px;
    cursor: pointer;
    transition: 0.18s ease;
    font-size: 13px;
  }
  .tab:hover { border-color: rgba(104, 212, 255, 0.4); color: #d9f7ff; }
  .tab.active {
    background: linear-gradient(135deg, rgba(104, 212, 255, 0.18), rgba(106, 240, 194, 0.14));
    border-color: rgba(104, 212, 255, 0.35);
    color: #effcff;
  }
  .panel { display: none; }
  .panel.active { display: block; }
  .field-stack { display: flex; flex-direction: column; gap: 14px; }
  .textarea, .select {
    width: 100%;
    border-radius: 16px;
    border: 1px solid rgba(104, 212, 255, 0.12);
    background: rgba(5, 10, 16, 0.9);
    color: #eff6ff;
    padding: 14px 16px;
    font-size: 14px;
    outline: none;
  }
  .textarea {
    min-height: 112px;
    resize: vertical;
    line-height: 1.65;
  }
  .textarea:focus, .select:focus {
    border-color: rgba(104, 212, 255, 0.5);
    box-shadow: 0 0 0 4px rgba(104, 212, 255, 0.08);
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .label {
    font-size: 12px;
    color: #7e8ba0;
    font-weight: 700;
    letter-spacing: 0.05em;
  }
  .checks {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 12px;
    padding: 14px 16px;
    border-radius: 16px;
    border: 1px solid rgba(104, 212, 255, 0.12);
    background: rgba(5, 10, 16, 0.9);
  }
  .checks label, .inline-check {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    color: #dbe7f4;
  }
  .hint {
    color: #708096;
    font-size: 12px;
    line-height: 1.7;
  }
  .actions { display: flex; flex-wrap: wrap; gap: 10px; }
  .btn {
    border-radius: 14px;
    padding: 11px 16px;
    border: 1px solid rgba(104, 212, 255, 0.14);
    cursor: pointer;
    font-size: 13px;
    font-weight: 700;
    transition: 0.18s ease;
    color: #edf8ff;
    background: rgba(255, 255, 255, 0.03);
  }
  .btn:hover { transform: translateY(-1px); border-color: rgba(104, 212, 255, 0.3); }
  .btn-primary {
    background: linear-gradient(135deg, #1b8fc5, #1fbe95);
    border-color: rgba(104, 212, 255, 0.24);
  }
  .btn-danger {
    background: rgba(255, 88, 88, 0.12);
    border-color: rgba(255, 88, 88, 0.24);
    color: #ffd3d3;
  }
  .btn-subtle {
    background: transparent;
    color: #9fb0c4;
  }
  .quick-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }
  .quick-btn {
    text-align: left;
    padding: 16px;
    border-radius: 18px;
    border: 1px solid rgba(104, 212, 255, 0.12);
    background: rgba(255, 255, 255, 0.02);
    cursor: pointer;
    color: #edf8ff;
  }
  .quick-btn strong { display: block; margin-bottom: 8px; font-size: 15px; }
  .quick-btn span { display: block; color: #7f8a9d; font-size: 12px; line-height: 1.6; }
  .queue-summary {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    margin-bottom: 14px;
  }
  .pill {
    border-radius: 999px;
    padding: 8px 12px;
    font-size: 12px;
    color: #aab7c9;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(104, 212, 255, 0.1);
  }
  .queue-list {
    display: flex;
    flex-direction: column;
    gap: 12px;
    max-height: 920px;
    overflow-y: auto;
    scrollbar-width: thin;
    scrollbar-color: rgba(104, 212, 255, 0.28) rgba(255, 255, 255, 0.04);
  }
  .recent-panel {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .recent-filter-row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .recent-filter-btn {
    border-radius: 999px;
    border: 1px solid rgba(104, 212, 255, 0.12);
    background: rgba(255, 255, 255, 0.03);
    color: #9fb0c4;
    padding: 7px 12px;
    font-size: 12px;
    font-weight: 700;
    cursor: pointer;
    transition: 0.18s ease;
  }
  .recent-filter-btn:hover {
    border-color: rgba(104, 212, 255, 0.28);
    color: #e7f8ff;
  }
  .recent-filter-btn.active {
    background: linear-gradient(135deg, rgba(104, 212, 255, 0.18), rgba(106, 240, 194, 0.12));
    border-color: rgba(104, 212, 255, 0.34);
    color: #effcff;
  }
  .recent-search {
    width: 100%;
    border-radius: 14px;
    border: 1px solid rgba(104, 212, 255, 0.12);
    background: rgba(5, 10, 16, 0.9);
    color: #eff6ff;
    padding: 12px 14px;
    font-size: 13px;
    outline: none;
  }
  .recent-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
    max-height: 240px;
    overflow-y: auto;
    padding-right: 2px;
    scrollbar-width: thin;
    scrollbar-color: rgba(104, 212, 255, 0.28) rgba(255, 255, 255, 0.04);
  }
  .recent-option {
    width: 100%;
    text-align: left;
    border-radius: 14px;
    border: 1px solid rgba(104, 212, 255, 0.08);
    background: rgba(6, 10, 15, 0.78);
    color: #dce8f5;
    padding: 10px 12px;
    cursor: pointer;
    transition: 0.18s ease;
  }
  .recent-option:hover {
    border-color: rgba(104, 212, 255, 0.24);
    background: rgba(10, 18, 28, 0.9);
  }
  .recent-option.active {
    border-color: rgba(104, 212, 255, 0.35);
    background: linear-gradient(135deg, rgba(104, 212, 255, 0.12), rgba(106, 240, 194, 0.08));
  }
  .recent-option-name {
    font-size: 12px;
    font-weight: 700;
    color: #eef8ff;
    line-height: 1.55;
    word-break: break-word;
    margin-bottom: 4px;
  }
  .recent-option-meta {
    font-size: 11px;
    color: #7f8a9d;
    line-height: 1.5;
  }
  .recent-detail {
    border-radius: 16px;
    padding: 13px 14px;
    background: rgba(6, 10, 15, 0.78);
    border: 1px solid rgba(104, 212, 255, 0.08);
  }
  .recent-name {
    font-size: 13px;
    font-weight: 700;
    color: #eef8ff;
    line-height: 1.55;
    word-break: break-word;
    margin-bottom: 8px;
  }
  .recent-meta {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 12px;
    color: #7f8a9d;
    line-height: 1.7;
  }
  .recent-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 12px;
  }
  .recent-empty {
    border-radius: 18px;
    padding: 14px 15px;
    background: rgba(6, 10, 15, 0.78);
    border: 1px solid rgba(104, 212, 255, 0.08);
    color: #7f8a9d;
    font-size: 12px;
    line-height: 1.75;
  }
  .queue-item {
    border-radius: 18px;
    padding: 14px 15px;
    background: rgba(6, 10, 15, 0.78);
    border: 1px solid rgba(104, 212, 255, 0.08);
  }
  .queue-item.running { border-color: rgba(255, 184, 76, 0.34); }
  .queue-item.completed { border-color: rgba(106, 240, 194, 0.22); }
  .queue-item.failed, .queue-item.stopped { border-color: rgba(255, 88, 88, 0.25); }
  .queue-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 8px;
  }
  .queue-name {
    font-size: 14px;
    font-weight: 700;
    color: #eef8ff;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .queue-status {
    flex-shrink: 0;
    border-radius: 999px;
    padding: 4px 10px;
    font-size: 11px;
    font-weight: 700;
    background: rgba(255, 255, 255, 0.05);
    color: #a6b5c9;
  }
  .queue-item.running .queue-status { color: #ffcf7d; }
  .queue-item.completed .queue-status { color: #7cf0c4; }
  .queue-item.failed .queue-status, .queue-item.stopped .queue-status { color: #ff9f9f; }
  .queue-meta {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px 12px;
    font-size: 12px;
    color: #7f8a9d;
    line-height: 1.6;
  }
  .queue-error {
    margin-top: 8px;
    font-size: 12px;
    color: #ff9f9f;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .queue-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 10px;
  }
  .queue-btn {
    border-radius: 999px;
    padding: 5px 10px;
    font-size: 11px;
    font-weight: 700;
    border: 1px solid rgba(104, 212, 255, 0.14);
    background: rgba(255, 255, 255, 0.03);
    color: #c9d7e7;
    cursor: pointer;
  }
  .queue-btn:hover {
    border-color: rgba(104, 212, 255, 0.3);
  }
  .queue-btn.danger {
    border-color: rgba(255, 88, 88, 0.24);
    color: #ffbbbb;
  }
  .queue-btn[disabled] {
    opacity: 0.35;
    cursor: not-allowed;
  }
  .toolbar-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 12px;
    flex-wrap: wrap;
  }
  .toolbar-actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .log-area {
    height: 430px;
    overflow-y: auto;
    border-radius: 18px;
    background: rgba(3, 7, 12, 0.94);
    border: 1px solid rgba(104, 212, 255, 0.1);
    padding: 16px 18px;
    font-family: "SF Mono", "Fira Code", Consolas, monospace;
    font-size: 12.5px;
    line-height: 1.92;
    scrollbar-width: thin;
    scrollbar-color: rgba(104, 212, 255, 0.28) rgba(255, 255, 255, 0.04);
  }
  .queue-list::-webkit-scrollbar,
  .recent-list::-webkit-scrollbar,
  .log-area::-webkit-scrollbar {
    width: 10px;
  }
  .queue-list::-webkit-scrollbar-track,
  .recent-list::-webkit-scrollbar-track,
  .log-area::-webkit-scrollbar-track {
    background: rgba(255, 255, 255, 0.04);
    border-radius: 999px;
  }
  .queue-list::-webkit-scrollbar-thumb,
  .recent-list::-webkit-scrollbar-thumb,
  .log-area::-webkit-scrollbar-thumb {
    background: linear-gradient(180deg, rgba(104, 212, 255, 0.38), rgba(106, 240, 194, 0.28));
    border-radius: 999px;
    border: 2px solid rgba(6, 10, 15, 0.7);
  }
  .queue-list::-webkit-scrollbar-thumb:hover,
  .recent-list::-webkit-scrollbar-thumb:hover,
  .log-area::-webkit-scrollbar-thumb:hover {
    background: linear-gradient(180deg, rgba(104, 212, 255, 0.5), rgba(106, 240, 194, 0.36));
  }
  .log-line { white-space: pre-wrap; word-break: break-word; color: #6c7b90; }
  .log-line.hl { color: #74dbff; font-weight: 700; }
  .log-line.ok { color: #74e0aa; }
  .log-line.err { color: #ff8f8f; }
  .log-line.warn { color: #ffc46b; }
  .log-line.idle { color: #8a97aa; }
  .status-bar {
    margin-top: 12px;
    display: grid;
    grid-template-columns: auto 1fr auto;
    gap: 12px;
    align-items: center;
    font-size: 12px;
    color: #7c8799;
  }
  .state {
    display: inline-flex;
    align-items: center;
    gap: 8px;
  }
  .dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: #5ec98d;
    flex-shrink: 0;
  }
  .dot.run { background: #ffb84c; animation: pulse 1.2s infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
  .task-info { color: #a8b7cb; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .muted { color: #708096; }
  @media (max-width: 1120px) {
    .layout { grid-template-columns: 1fr; }
  }
  @media (max-width: 720px) {
    body { padding: 16px 12px 28px; }
    .hero, .card { border-radius: 18px; padding: 18px; }
    .grid, .queue-meta, .quick-grid, .status-bar { grid-template-columns: 1fr; }
    .toolbar-row { align-items: stretch; }
  }
</style>
</head>
<body>
<div class="page">
  <section class="hero">
    <h1><span>ZhihuScraper</span> 控制台</h1>
    <p>支持问题回答、用户主页、热榜与推荐流抓取。现在可以批量添加任务进入队列，支持快速预览、保守模式、单文件离线 HTML，以及任务完成后的自动熄屏或休眠。</p>
  </section>

  <div class="layout">
    <main class="stack">
      <section class="card">
        <div class="card-head">
          <div class="card-title">发起任务</div>
        </div>

        <div class="tabs">
          <button type="button" class="tab active" data-p="question">问题回答</button>
          <button type="button" class="tab" data-p="user">用户主页</button>
          <button type="button" class="tab" data-p="quick">快捷入口</button>
        </div>

        <div id="p-question" class="panel active">
          <div class="field-stack">
            <div class="field">
              <div class="label">问题 ID / 链接（支持多行，一行一个）</div>
              <textarea class="textarea" id="q-input" placeholder="输入问题 ID、问题链接或回答链接，例如：
https://www.zhihu.com/question/2009611085918013365
https://www.zhihu.com/question/2009611085918013365/answer/123456789"></textarea>
            </div>
            <div class="grid">
              <div class="field">
                <div class="label">导出模式</div>
                <select class="select" id="q-mode">
                  <option value="full">完整内容（HTML/图片）</option>
                  <option value="text">纯文字 JSON</option>
                  <option value="fast">快速预览</option>
                </select>
              </div>
              <div class="field">
                <div class="label">抓取策略</div>
                <select class="select" id="q-profile">
                  <option value="standard">标准模式</option>
                  <option value="conservative">保守模式（更慢、更稳）</option>
                </select>
              </div>
              <div class="field">
                <div class="label">HTML 导出</div>
                <select class="select" id="q-html-variant">
                  <option value="dir">目录模式（HTML + assets）</option>
                  <option value="single">单文件离线 HTML</option>
                </select>
              </div>
              <div class="field">
                <div class="label">任务完成后</div>
                <select class="select" id="q-post-action">
                  <option value="none">无</option>
                  <option value="display_off">熄灭屏幕</option>
                  <option value="sleep">系统休眠</option>
                </select>
              </div>
            </div>
            <div class="checks">
              <label><input type="checkbox" id="q-force" /> 忽略本地已存在内容，强制重新抓取</label>
            </div>
            <div class="actions">
          <button type="button" class="btn btn-primary" id="btn-enqueue-question">加入队列</button>
            </div>
            <div class="hint">问题任务支持完整链接自动识别。`快速预览` 会优先拿列表结果，速度更快，但完整度可能低于完整模式。</div>
          </div>
        </div>

        <div id="p-user" class="panel">
          <div class="field-stack">
            <div class="field">
              <div class="label">用户 token / 链接（支持多行，一行一个）</div>
              <textarea class="textarea" id="u-input" placeholder="输入用户主页链接或 token，例如：
https://www.zhihu.com/people/ming--li
ming--li"></textarea>
            </div>
            <div class="grid">
              <div class="field">
                <div class="label">导出模式</div>
                <select class="select" id="u-mode">
                  <option value="full">完整内容（HTML/图片）</option>
                  <option value="text">纯文字 JSON</option>
                  <option value="fast">快速预览</option>
                </select>
              </div>
              <div class="field">
                <div class="label">抓取策略</div>
                <select class="select" id="u-profile">
                  <option value="standard">标准模式</option>
                  <option value="conservative">保守模式（更慢、更稳）</option>
                </select>
              </div>
              <div class="field">
                <div class="label">HTML 导出</div>
                <select class="select" id="u-html-variant">
                  <option value="dir">目录模式（HTML + assets）</option>
                  <option value="single">单文件离线 HTML</option>
                </select>
              </div>
              <div class="field">
                <div class="label">任务完成后</div>
                <select class="select" id="u-post-action">
                  <option value="none">无</option>
                  <option value="display_off">熄灭屏幕</option>
                  <option value="sleep">系统休眠</option>
                </select>
              </div>
            </div>
            <div class="field">
              <div class="label">用户内容筛选</div>
              <div class="checks" id="u-types">
                <label><input type="checkbox" value="answer" checked /> 回答</label>
                <label><input type="checkbox" value="article" checked /> 文章</label>
                <label><input type="checkbox" value="pin" checked /> 想法</label>
              </div>
            </div>
            <div class="checks">
              <label><input type="checkbox" id="u-force" /> 忽略本地已存在内容，强制重新抓取</label>
            </div>
            <div class="actions">
          <button type="button" class="btn btn-primary" id="btn-enqueue-user">加入队列</button>
            </div>
            <div class="hint">用户任务支持完整主页链接自动识别。`完整内容` 会先抓列表，再逐条补正文，通常比问题抓取更慢。</div>
          </div>
        </div>

        <div id="p-quick" class="panel">
          <div class="quick-grid">
            <button type="button" class="quick-btn" id="btn-quick-hot">
              <strong>热榜</strong>
              <span>抓取知乎实时热门话题，保存为 `hot-list.json`。</span>
            </button>
            <button type="button" class="quick-btn" id="btn-quick-recommend">
              <strong>推荐流</strong>
              <span>抓取推荐内容，保存为 `recommend.json`。</span>
            </button>
          </div>
        </div>
      </section>

      <section class="card">
        <div class="toolbar-row">
          <div class="card-title">实时日志</div>
          <div class="toolbar-actions">
            <button type="button" class="btn btn-subtle" id="btn-follow">跟随日志：开</button>
            <button type="button" class="btn btn-subtle" onclick="scrollLogToBottom()">回到底部</button>
            <button type="button" class="btn btn-subtle" onclick="copyLogs()" title="复制日志">⧉</button>
            <button type="button" class="btn btn-subtle" id="btn-pause" style="display:none">暂停</button>
            <button type="button" class="btn btn-danger" id="btn-stop" style="display:none">终止当前任务</button>
          </div>
        </div>
        <div class="log-area" id="log-area"></div>
        <div class="status-bar">
          <div class="state"><span class="dot" id="dot"></span><span id="st">空闲</span></div>
          <div class="task-info" id="task-info">队列空闲，等待新任务</div>
          <div class="muted" id="lc">0 条日志</div>
        </div>
      </section>
    </main>

    <aside class="stack">
      <section class="card">
        <div class="card-head">
          <div class="card-title">任务队列</div>
        </div>
        <div class="queue-summary" id="queue-summary">
          <span class="pill">排队中 0</span>
          <span class="pill">运行中 0</span>
          <span class="pill">已完成 0</span>
        </div>
        <div class="queue-list" id="queue-list"></div>
      </section>

      <section class="card">
        <div class="card-head">
          <div class="card-title">最近本地 HTML</div>
        </div>
        <div class="recent-panel" id="recent-html-panel">
          <div class="recent-empty">还没有生成本地 HTML。完成一次问题页或用户页导出后，会在这里显示最近的离线浏览页。</div>
        </div>
      </section>
    </aside>
  </div>
</div>

<script>
  let isRun = false;
  let paused = false;
  let autoFollow = true;
  let recentHtmlItems = [];
  let recentHtmlSelectedPath = '';
  let recentDeleteArmed = false;
  let recentHtmlSearch = '';
  let recentHtmlTypeFilter = 'all';
  let recentHtmlScrollTop = 0;
  let recentHtmlSignature = '';
  const logArea = document.getElementById('log-area');
  const qInput = document.getElementById('q-input');
  const uInput = document.getElementById('u-input');
  const userTypeInputs = Array.from(document.querySelectorAll('#u-types input[type="checkbox"]'));
  const TASK_NAMES = {question: '问题回答', user: '用户主页', 'hot-list': '热榜', recommend: '推荐流'};
  const STATUS_TEXT = {pending: '排队中', running: '执行中', completed: '已完成', failed: '失败', stopped: '已停止'};

  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(node => node.classList.remove('active'));
      document.querySelectorAll('.panel').forEach(node => node.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById('p-' + tab.dataset.p).classList.add('active');
    });
  });

  function classifyLog(text) {
    if (text.includes('✓')) return 'ok';
    if (text.includes('✗') || text.includes('错误')) return 'err';
    if (text.includes('⚠')) return 'warn';
    if (text.includes('▶')) return 'hl';
    if (text.includes('空闲')) return 'idle';
    return '';
  }

  function buildLogNode(text) {
    const line = document.createElement('div');
    line.className = 'log-line ' + classifyLog(text);
    const filePattern = new RegExp('(/output/[^\\\\s]+|[A-Za-z]:[\\\\/][^\\\\s]+\\\\.html|/[^\\\\s]+\\\\.html)');
    const match = text.match(filePattern);
    if (!match) {
      line.textContent = text;
      return line;
    }
    const filePath = match[0];
    const idx = text.indexOf(filePath);
    line.append(document.createTextNode(text.slice(0, idx)));
    const link = document.createElement('a');
    const outputIdx = filePath.indexOf('/output/');
    link.href = outputIdx >= 0 ? filePath.slice(outputIdx) : '/file?path=' + encodeURIComponent(filePath);
    link.target = '_blank';
    link.rel = 'noreferrer';
    link.textContent = filePath;
    line.append(link);
    line.append(document.createTextNode(text.slice(idx + filePath.length)));
    return line;
  }

  function refreshLogCount() {
    document.getElementById('lc').textContent = logArea.childElementCount + ' 条日志';
  }

  function appendLog(text) {
    const distanceFromBottom = logArea.scrollHeight - logArea.scrollTop - logArea.clientHeight;
    const nearBottom = distanceFromBottom < 24;
    logArea.appendChild(buildLogNode(text));
    if (autoFollow && nearBottom) {
      logArea.scrollTop = logArea.scrollHeight;
    }
    refreshLogCount();
  }

  function syncFollowButton() {
    document.getElementById('btn-follow').textContent = '跟随日志：' + (autoFollow ? '开' : '关');
  }

  function toggleFollow() {
    autoFollow = !autoFollow;
    if (autoFollow) {
      logArea.scrollTop = logArea.scrollHeight;
    }
    syncFollowButton();
  }

  function scrollLogToBottom() {
    autoFollow = true;
    logArea.scrollTop = logArea.scrollHeight;
    syncFollowButton();
  }

  logArea.addEventListener('scroll', () => {
    const nearBottom = logArea.scrollHeight - logArea.scrollTop - logArea.clientHeight < 24;
    if (!nearBottom && autoFollow) {
      autoFollow = false;
      syncFollowButton();
    } else if (nearBottom && !autoFollow) {
      autoFollow = true;
      syncFollowButton();
    }
  });

  async function copyLogs() {
    const text = Array.from(logArea.querySelectorAll('.log-line')).map(node => node.textContent).join('\\n');
    try {
      await navigator.clipboard.writeText(text);
      appendLog('✓ 已复制当前日志内容', 'ok');
    } catch (error) {
      appendLog('✗ 复制日志失败: ' + error.message, 'err');
    }
  }

  function selectedUserTypes() {
    return userTypeInputs.filter(node => node.checked).map(node => node.value);
  }

  async function enqueue(cmd, arg, mode='full', types='', profile='standard', htmlVariant='dir', postAction='none', force=false) {
    if (!['hot-list', 'recommend'].includes(cmd) && !arg.trim()) {
      appendLog('⚠ 请输入有效的 ID 或链接', 'warn');
      return;
    }
    try {
      const params = new URLSearchParams({
        cmd,
        arg,
        mode,
        types,
        profile,
        html_variant: htmlVariant,
        post_action: postAction,
        force: force ? '1' : '0',
      });
      const response = await fetch('/scrape?' + params.toString(), { method: 'POST' });
      const payload = await response.json();
      if (payload.status === 'error') {
        appendLog('✗ ' + payload.error, 'err');
      }
    } catch (error) {
      appendLog('✗ 请求失败: ' + error.message, 'err');
    }
  }

  function enqueueQuestion() {
    enqueue(
      'question',
      qInput.value,
      document.getElementById('q-mode').value,
      '',
      document.getElementById('q-profile').value,
      document.getElementById('q-html-variant').value,
      document.getElementById('q-post-action').value,
      document.getElementById('q-force').checked,
    );
  }

  function enqueueUser() {
    const types = selectedUserTypes();
    if (!types.length) {
      appendLog('⚠ 至少选择一种用户内容类型', 'warn');
      return;
    }
    enqueue(
      'user',
      uInput.value,
      document.getElementById('u-mode').value,
      types.join(','),
      document.getElementById('u-profile').value,
      document.getElementById('u-html-variant').value,
      document.getElementById('u-post-action').value,
      document.getElementById('u-force').checked,
    );
  }

  function enqueueQuick(cmd) {
    enqueue(cmd, '', 'text', '', 'standard', 'dir', 'none');
  }

  async function togglePause() {
    if (!isRun) return;
    try {
      const endpoint = paused ? '/resume' : '/pause';
      await fetch(endpoint, { method: 'POST' });
      paused = !paused;
      document.getElementById('btn-pause').textContent = paused ? '继续' : '暂停';
      appendLog(paused ? '⏸ 已暂停当前任务' : '▶ 已继续当前任务');
    } catch (_) {}
  }

  async function stopTask() {
    if (!isRun) return;
    try {
      await fetch('/stop', { method: 'POST' });
      appendLog('⚠ 已请求停止当前任务', 'warn');
    } catch (_) {}
  }

  async function queueDelete(id) {
    try {
      const response = await fetch('/queue/delete?id=' + encodeURIComponent(String(id)), { method: 'POST' });
      const payload = await response.json();
      if (!payload.ok) {
        appendLog('⚠ ' + payload.message, 'warn');
      }
      await syncStatus();
    } catch (error) {
      appendLog('✗ 删除队列任务失败: ' + error.message, 'err');
    }
  }

  async function queueMove(id, direction) {
    try {
      const params = new URLSearchParams({ id: String(id), direction });
      const response = await fetch('/queue/move?' + params.toString(), { method: 'POST' });
      const payload = await response.json();
      if (!payload.ok) {
        appendLog('⚠ ' + payload.message, 'warn');
      }
      await syncStatus();
    } catch (error) {
      appendLog('✗ 调整任务顺序失败: ' + error.message, 'err');
    }
  }

  function currentRecentHtmlItem() {
    return recentHtmlItems.find(item => item.path === recentHtmlSelectedPath) || null;
  }

  function resetRecentDeleteState() {
    recentDeleteArmed = false;
    const button = document.getElementById('btn-recent-delete');
    if (button) button.textContent = '删除 HTML + 图片';
  }

  function renderQueue(tasks) {
    const queueList = document.getElementById('queue-list');
    const summary = document.getElementById('queue-summary');
    const counts = { pending: 0, running: 0, completed: 0 };
    tasks.forEach(task => {
      if (task.status in counts) counts[task.status] += 1;
    });
    summary.innerHTML = `
      <span class="pill">排队中 ${counts.pending}</span>
      <span class="pill">运行中 ${counts.running}</span>
      <span class="pill">已完成 ${counts.completed}</span>
    `;
    if (!tasks.length) {
      queueList.innerHTML = '<div class="queue-item"><div class="queue-name">当前没有任务</div><div class="queue-meta"><span>添加问题链接、用户链接或快捷任务后，会在这里显示。</span></div></div>';
      return;
    }
    queueList.innerHTML = tasks.map(task => {
      const name = TASK_NAMES[task.cmd] || task.cmd;
      const target = task.label || task.raw_arg || task.arg || '-';
      const error = task.error ? `<div class="queue-error">${escapeHtml(task.error)}</div>` : '';
      let timeMeta = '';
      if (task.status === 'pending') {
        timeMeta = `
            <span>提交时间：${escapeHtml(task.submitted_text || '-')}</span>
            <span>预计开始：${escapeHtml(task.estimated_start_text || '-')}</span>
            <span>预计完成：${escapeHtml(task.estimated_finish_text || '-')}</span>
        `;
      } else if (task.status === 'running') {
        timeMeta = `
            <span>开始时间：${escapeHtml(task.started_text || '-')}</span>
            <span>已运行：${escapeHtml(task.actual_duration_text || '-')}</span>
            <span>当前阶段：${escapeHtml(task.latest_stage || '-')}</span>
            <span>最新进度：${escapeHtml(task.latest_progress_text || '-')}</span>
            <span>最新预计完成：${escapeHtml(task.latest_eta_text || task.estimated_finish_text || '-')}</span>
        `;
      } else {
        timeMeta = `
            <span>开始时间：${escapeHtml(task.started_text || '-')}</span>
            <span>完成时间：${escapeHtml(task.finished_text || '-')}</span>
            <span>实际耗时：${escapeHtml(task.actual_duration_text || '-')}</span>
        `;
      }
      const actions = [];
      if (task.status === 'pending') {
        actions.push(`<button type="button" class="queue-btn" onclick="queueMove(${task.id}, 'up')" ${task.can_move_up ? '' : 'disabled'}>上移</button>`);
        actions.push(`<button type="button" class="queue-btn" onclick="queueMove(${task.id}, 'down')" ${task.can_move_down ? '' : 'disabled'}>下移</button>`);
      }
      if (task.can_delete) {
        actions.push(`<button type="button" class="queue-btn danger" onclick="queueDelete(${task.id})">删除</button>`);
      }
      return `
        <div class="queue-item ${task.status}">
          <div class="queue-top">
            <div class="queue-name">#${task.id} ${escapeHtml(target)}</div>
            <div class="queue-status">${STATUS_TEXT[task.status] || task.status}</div>
          </div>
          <div class="queue-meta">
            <span>类型：${escapeHtml(name)}</span>
            <span>目标：${escapeHtml(task.arg || task.raw_arg || '-')}</span>
            <span>模式：${escapeHtml(task.mode)}</span>
            <span>策略：${escapeHtml(task.profile)}</span>
            <span>HTML：${escapeHtml(task.html_variant)}</span>
            <span>${task.status === 'pending' ? '初始预估' : '初始预估耗时'}：${escapeHtml(task.estimated_duration_text)}</span>
            ${timeMeta}
          </div>
          ${actions.length ? `<div class="queue-actions">${actions.join('')}</div>` : ''}
          ${error}
        </div>
      `;
    }).join('');
  }

  function renderRecentHtml(items) {
    const panel = document.getElementById('recent-html-panel');
    if (!panel) return;
    recentHtmlItems = Array.isArray(items) ? items : [];
    const searchActive = document.activeElement && document.activeElement.id === 'recent-html-search';
    const activeSearch = searchActive ? document.getElementById('recent-html-search') : null;
    const searchSelectionStart = activeSearch ? activeSearch.selectionStart : null;
    const searchSelectionEnd = activeSearch ? activeSearch.selectionEnd : null;
    if (!recentHtmlItems.length) {
      recentHtmlSelectedPath = '';
      resetRecentDeleteState();
      panel.innerHTML = '<div class="recent-empty">还没有生成本地 HTML。完成一次问题页或用户页导出后，会在这里显示最近的离线浏览页。</div>';
      return;
    }
    const filteredItems = recentHtmlItems.filter(item => {
      const keyword = recentHtmlSearch.trim().toLowerCase();
      if (recentHtmlTypeFilter !== 'all' && item.kind !== recentHtmlTypeFilter) return false;
      if (!keyword) return true;
      const haystack = `${item.kind} ${item.name || ''} ${item.modified_text || ''}`.toLowerCase();
      return haystack.includes(keyword);
    });
    if (!filteredItems.length) {
      recentHtmlSelectedPath = '';
      resetRecentDeleteState();
      panel.innerHTML = `
        <div class="recent-filter-row">
          <button type="button" class="recent-filter-btn${recentHtmlTypeFilter === 'all' ? ' active' : ''}" data-kind="all">全部</button>
          <button type="button" class="recent-filter-btn${recentHtmlTypeFilter === 'question' ? ' active' : ''}" data-kind="question">问题</button>
          <button type="button" class="recent-filter-btn${recentHtmlTypeFilter === 'user' ? ' active' : ''}" data-kind="user">用户</button>
        </div>
        <input class="recent-search" id="recent-html-search" placeholder="搜索标题、类型或时间" value="${escapeHtml(recentHtmlSearch)}" />
        <div class="recent-empty">没有匹配的本地 HTML。换个关键词试试。</div>
      `;
      document.querySelectorAll('.recent-filter-btn').forEach((node) => {
        node.addEventListener('click', () => {
          recentHtmlTypeFilter = node.dataset.kind || 'all';
          recentHtmlScrollTop = 0;
          renderRecentHtml(recentHtmlItems);
        });
      });
      document.getElementById('recent-html-search').addEventListener('input', (event) => {
        recentHtmlSearch = event.target.value;
        renderRecentHtml(recentHtmlItems);
      });
      if (searchActive) {
        const nextSearch = document.getElementById('recent-html-search');
        if (nextSearch) {
          nextSearch.focus();
          if (searchSelectionStart !== null && searchSelectionEnd !== null) {
            nextSearch.setSelectionRange(searchSelectionStart, searchSelectionEnd);
          }
        }
      }
      return;
    }
    if (!filteredItems.some(item => item.path === recentHtmlSelectedPath)) {
      recentHtmlSelectedPath = filteredItems[0].path;
      resetRecentDeleteState();
    }
    const selected = currentRecentHtmlItem() || filteredItems[0];
    const listItems = filteredItems.map(item => {
      const kindLabel = item.kind === 'question' ? '问题' : '用户';
      const active = item.path === selected.path ? ' active' : '';
      return `
        <button type="button" class="recent-option${active}" data-path="${escapeHtml(item.path)}">
          <div class="recent-option-name">${escapeHtml(item.name || '-')}</div>
          <div class="recent-option-meta">${escapeHtml(kindLabel)} · ${escapeHtml(item.modified_text || '-')}</div>
        </button>
      `;
    }).join('');
    panel.innerHTML = `
      <div class="recent-filter-row">
        <button type="button" class="recent-filter-btn${recentHtmlTypeFilter === 'all' ? ' active' : ''}" data-kind="all">全部</button>
        <button type="button" class="recent-filter-btn${recentHtmlTypeFilter === 'question' ? ' active' : ''}" data-kind="question">问题</button>
        <button type="button" class="recent-filter-btn${recentHtmlTypeFilter === 'user' ? ' active' : ''}" data-kind="user">用户</button>
      </div>
      <input class="recent-search" id="recent-html-search" placeholder="搜索标题、类型或时间" value="${escapeHtml(recentHtmlSearch)}" />
      <div class="recent-list" id="recent-html-list">${listItems}</div>
      <div class="recent-detail">
        <div class="recent-name">${escapeHtml(selected.name || '-')}</div>
        <div class="recent-meta">
          <span>类型：${escapeHtml(selected.kind === 'question' ? '问题' : '用户')}</span>
          <span>最近生成：${escapeHtml(selected.modified_text || '-')}</span>
        </div>
        <div class="recent-actions">
          <button type="button" class="btn btn-subtle" id="btn-recent-open">打开浏览页</button>
          <button type="button" class="btn btn-danger" id="btn-recent-delete">删除 HTML + 图片</button>
        </div>
      </div>
    `;
    document.querySelectorAll('.recent-filter-btn').forEach((node) => {
      node.addEventListener('click', () => {
        recentHtmlTypeFilter = node.dataset.kind || 'all';
        recentHtmlScrollTop = 0;
        renderRecentHtml(recentHtmlItems);
      });
    });
    document.getElementById('recent-html-search').addEventListener('input', (event) => {
      recentHtmlSearch = event.target.value;
      recentHtmlScrollTop = 0;
      renderRecentHtml(recentHtmlItems);
    });
    const recentList = document.getElementById('recent-html-list');
    if (recentList) {
      recentList.scrollTop = recentHtmlScrollTop;
      recentList.addEventListener('scroll', () => {
        recentHtmlScrollTop = recentList.scrollTop;
      });
    }
    document.querySelectorAll('.recent-option').forEach((node) => {
      node.addEventListener('click', () => {
        recentHtmlSelectedPath = node.dataset.path || '';
        resetRecentDeleteState();
        renderRecentHtml(recentHtmlItems);
      });
    });
    document.getElementById('btn-recent-open').addEventListener('click', openRecentHtml);
    document.getElementById('btn-recent-delete').addEventListener('click', deleteRecentHtml);
    if (searchActive) {
      const nextSearch = document.getElementById('recent-html-search');
      if (nextSearch) {
        nextSearch.focus();
        if (searchSelectionStart !== null && searchSelectionEnd !== null) {
          nextSearch.setSelectionRange(searchSelectionStart, searchSelectionEnd);
        }
      }
    }
  }

  function openRecentHtml() {
    const item = currentRecentHtmlItem();
    if (!item || !item.web_path) {
      appendLog('⚠ 当前没有可打开的本地 HTML', 'warn');
      return;
    }
    window.open(item.web_path, '_blank', 'noopener,noreferrer');
  }

  async function deleteRecentHtml() {
    const item = currentRecentHtmlItem();
    if (!item) {
      appendLog('⚠ 当前没有可删除的本地 HTML', 'warn');
      return;
    }
    const button = document.getElementById('btn-recent-delete');
    if (!recentDeleteArmed) {
      recentDeleteArmed = true;
      if (button) button.textContent = '再次确认删除';
      appendLog('⚠ 请再次点击“删除 HTML + 图片”确认删除本地浏览页与离线图片', 'warn');
      return;
    }
    try {
      const response = await fetch('/recent-html/delete?path=' + encodeURIComponent(item.path), { method: 'POST' });
      const payload = await response.json();
      resetRecentDeleteState();
      if (!payload.ok) {
        appendLog('⚠ ' + payload.message, 'warn');
      }
      await syncStatus();
    } catch (error) {
      resetRecentDeleteState();
      appendLog('✗ 删除本地 HTML 失败: ' + error.message, 'err');
    }
  }

  function updateRunningState(payload) {
    isRun = !!payload.running;
    paused = !!payload.paused;
    const dot = document.getElementById('dot');
    const st = document.getElementById('st');
    const taskInfo = document.getElementById('task-info');
    const pauseBtn = document.getElementById('btn-pause');
    const stopBtn = document.getElementById('btn-stop');
    if (isRun) {
      dot.className = 'dot run';
      st.textContent = paused ? '已暂停' : '执行中';
      const currentTask = TASK_NAMES[payload.task] || payload.task || '任务';
      const queue = Array.isArray(payload.queue) ? payload.queue : [];
      const pendingCount = queue.filter(item => item.status === 'pending').length;
      const runningItem = queue.find(item => item.status === 'running');
      const stage = runningItem && runningItem.latest_stage ? ' · ' + runningItem.latest_stage : '';
      const progress = runningItem && runningItem.latest_progress_text ? ' · ' + runningItem.latest_progress_text : '';
      const elapsed = runningItem && runningItem.latest_elapsed_text
        ? '，已用时 ' + runningItem.latest_elapsed_text
        : (runningItem && runningItem.actual_duration_text ? '，已运行 ' + runningItem.actual_duration_text : '');
      const eta = runningItem && (runningItem.latest_eta_text || runningItem.estimated_finish_text)
        ? '，预计完成 ' + (runningItem.latest_eta_text || runningItem.estimated_finish_text)
        : '';
      taskInfo.textContent = `${currentTask}${payload.arg ? ' · ' + payload.arg : ''}${stage}${progress}${elapsed}${eta}${pendingCount ? ' · 后续排队 ' + pendingCount + ' 项' : ''}`;
      pauseBtn.style.display = 'inline-block';
      pauseBtn.textContent = paused ? '继续' : '暂停';
      stopBtn.style.display = 'inline-block';
    } else {
      dot.className = 'dot';
      st.textContent = '空闲';
      taskInfo.textContent = '队列空闲，等待新任务';
      pauseBtn.style.display = 'none';
      stopBtn.style.display = 'none';
    }
  }

  function escapeHtml(value) {
    return String(value || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function exposeHandlers() {
    window.enqueueQuestion = enqueueQuestion;
    window.enqueueUser = enqueueUser;
    window.enqueueQuick = enqueueQuick;
    window.togglePause = togglePause;
    window.stopTask = stopTask;
    window.queueDelete = queueDelete;
    window.queueMove = queueMove;
    window.toggleFollow = toggleFollow;
    window.scrollLogToBottom = scrollLogToBottom;
    window.copyLogs = copyLogs;
  }

  function wireButtons() {
    const bind = (id, handler) => {
      const node = document.getElementById(id);
      if (!node) return;
      node.addEventListener('click', (event) => {
        event.preventDefault();
        handler();
      });
    };
    bind('btn-enqueue-question', enqueueQuestion);
    bind('btn-enqueue-user', enqueueUser);
    bind('btn-quick-hot', () => enqueueQuick('hot-list'));
    bind('btn-quick-recommend', () => enqueueQuick('recommend'));
    bind('btn-follow', toggleFollow);
    bind('btn-pause', togglePause);
    bind('btn-stop', stopTask);
  }

  window.addEventListener('error', (event) => {
    const target = document.getElementById('task-info');
    const message = '前端脚本错误: ' + (event.message || '未知错误');
    if (target) {
      target.textContent = message;
    }
    try {
      appendLog('✗ ' + message);
    } catch (_) {}
  });

  const eventSource = new EventSource('/logs');
  eventSource.onmessage = (event) => appendLog(event.data);

  async function syncStatus() {
    try {
      const response = await fetch('/status');
      const payload = await response.json();
      updateRunningState(payload);
      renderQueue(payload.queue || []);
      const nextRecentItems = payload.recent_html || [];
      const nextSignature = JSON.stringify(nextRecentItems.map(item => [item.path, item.modified_at, item.modified_text]));
      if (nextSignature !== recentHtmlSignature) {
        recentHtmlSignature = nextSignature;
        renderRecentHtml(nextRecentItems);
      } else {
        recentHtmlItems = nextRecentItems;
      }
    } catch (_) {}
  }

  exposeHandlers();
  wireButtons();
  syncFollowButton();
  syncStatus();
  setInterval(syncStatus, 1000);
</script>
</body>
</html>
"""


def _find_available_port(preferred_port: int, max_tries: int = 20) -> int:
    for port in range(preferred_port, preferred_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("localhost", port))
                return port
            except OSError:
                continue
    raise OSError(f"localhost:{preferred_port}-{preferred_port + max_tries - 1} 均不可用")


def main():
    global _server_instance
    _load_local_env()
    requested_port = int(os.getenv("GUI_PORT", "8080"))
    try:
        port = requested_port if "GUI_PORT" in os.environ else _find_available_port(requested_port)
        server = QuietThreadingHTTPServer(("localhost", port), GUIHandler)
    except OSError as exc:
        print(f"启动 GUI 失败: 无法绑定 localhost:{requested_port} ({exc})", file=sys.stderr)
        print("可设置环境变量 GUI_PORT 指定端口，例如: GUI_PORT=8081 venv/bin/python gui.py", file=sys.stderr)
        raise SystemExit(1) from exc
    _server_instance = server
    print(f"=" * 40)
    print(f"  知乎爬虫 GUI")
    if port != requested_port and "GUI_PORT" not in os.environ:
        print(f"  端口 {requested_port} 已占用，自动切换到 {port}")
    print(f"  打开: http://localhost:{port}")
    print(f"  Cookie: {'已加载' if os.getenv('ZHIHU_COOKIE') else '未配置'}")
    print(f"  日志: {_ensure_log_file()}")
    print(f"  按 Ctrl+C 停止")
    print(f"=" * 40)
    add_log(f"✓ GUI 已启动: http://localhost:{port}")
    add_log(f"✓ 日志文件: {_ensure_log_file()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
