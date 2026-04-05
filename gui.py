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
import socket
import sys
import queue
import mimetypes
import re
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import OUTPUT_DIR
from input_normalizer import normalize_question_input, normalize_user_input

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
                "python": sys.executable[:50]
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
            conservative = qs.get("conservative", ["0"])[0]

            running, _, _ = _snapshot_task_state()
            if running:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "error": "有任务正在运行"}).encode("utf-8"))
                return

            _set_task_state(True, cmd, arg)
            _stop_event.clear()
            _pause_event.clear()
            threading.Thread(target=_run_scrape, args=(cmd, arg, mode, types, conservative == "1"), daemon=True).start()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "started"}).encode("utf-8"))

        else:
            self.send_error(404)


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        exc_type, exc, _ = sys.exc_info()
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def _scrape_question(question_id: str, content_mode: str, conservative_mode: bool):
    from scraper.question import QuestionScraper
    from storage import prepare_question_batch_dir, save_question_batch, merge_question_batches, save_question
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
    add_log(f"✓ 抓取模式: {'纯文字 JSON' if content_mode == 'text' else '完整内容（HTML/图片）'}")
    add_log(f"✓ 抓取节奏: {'保守模式（更慢、更稳）' if conservative_mode else '标准模式'}")

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
            return

        add_log(f"✓ 问题全名：{q.title}")
        add_log(f"✓ 已抓回答：{len(q.answers)} / 声明总数：{q.answer_count}")
        path = merge_question_batches(question_id) or save_question(q, question_id)
        html_path = render_question_html(q, conservative_mode=conservative_mode, progress_callback=add_log)
        add_log(f"✓ 已保存至: {path}")
        add_log(f"✓ 浏览页: {html_path}")
        add_log(f"✓ 完成！共爬取 {len(q.answers)} 条回答")

    except Exception as e:
        add_log(f"✗ 错误: {e}")
        import traceback
        add_log(traceback.format_exc())
        traceback.print_exc()
    finally:
        try:
            scraper.close()
        except Exception:
            pass
        scraper_logger.handlers = orig_handlers


def _scrape_user(user_id: str, content_mode: str, content_types: list[str], conservative_mode: bool):
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
    add_log(f"✓ 抓取模式: {'纯文字 JSON' if content_mode == 'text' else '完整内容（HTML/图片）'}")
    add_log(f"✓ 内容类型: {', '.join(content_types)}")
    add_log(f"✓ 抓取节奏: {'保守模式（更慢、更稳）' if conservative_mode else '标准模式'}")
    add_log("✓ 用户主页抓取分两步：先抓内容列表，再补正文详情；完整模式下可能持续较久")

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
            return

        add_log(f"✓ 用户名：{u.name}")
        add_log(f"✓ 已抓内容：{len(u.activities)} 条")
        rich_count = sum(1 for item in u.activities if item.content_html)
        add_log(f"✓ 正文补全：成功 {rich_count} 条，失败 {len(u.activities) - rich_count} 条")
        path = save_user(u, user_id)
        html_path = render_user_html(u, conservative_mode=conservative_mode, progress_callback=add_log)
        add_log(f"✓ 已保存至: {path}")
        add_log(f"✓ 浏览页: {html_path}")
        add_log(f"✓ 完成！")

    except Exception as e:
        add_log(f"✗ 错误: {e}")
        import traceback
        add_log(traceback.format_exc())
        traceback.print_exc()
    finally:
        try:
            scraper.close()
        except Exception:
            pass
        scraper_logger.handlers = orig_handlers


def _run_scrape(cmd, arg, mode="full", raw_types="", conservative_mode=False):
    _pause_event.clear()
    _stop_event.clear()

    try:
        _load_local_env()

        from scraper import FeedScraper
        content_types = [item.strip() for item in raw_types.split(",") if item.strip()]

        if cmd == "question":
            question_id = normalize_question_input(arg)
            if question_id != arg:
                add_log(f"✓ 已从链接识别问题 ID: {question_id}")
            _scrape_question(question_id, mode, conservative_mode)
        elif cmd == "user":
            user_id = normalize_user_input(arg)
            if user_id != arg:
                add_log(f"✓ 已从链接识别用户 ID: {user_id}")
            _scrape_user(user_id, mode, content_types or ["answer", "article", "pin"], conservative_mode)
        elif cmd == "hot-list":
            add_log("✓ 正在连接知乎...")
            scraper = FeedScraper()
            items = scraper.fetch_hot_list(limit=30)
            add_log(f"✓ 获取成功，共 {len(items)} 条")
            os.makedirs("output", exist_ok=True)
            path = "output/hot-list.json"
            import json as j2
            with open(path, "w", encoding="utf-8") as f:
                j2.dump(items, f, ensure_ascii=False, indent=2, default=str)
            add_log(f"✓ 已保存至: {path}")
            add_log(f"✓ 完成！")
        elif cmd == "recommend":
            add_log("✓ 正在连接知乎...")
            scraper = FeedScraper()
            items = scraper.fetch_recommend(page=0, per_page=20)
            add_log(f"✓ 获取成功，共 {len(items)} 条")
            os.makedirs("output", exist_ok=True)
            path = "output/recommend.json"
            import json as j2
            with open(path, "w", encoding="utf-8") as f:
                j2.dump(items, f, ensure_ascii=False, indent=2, default=str)
            add_log(f"✓ 已保存至: {path}")
            add_log(f"✓ 完成！")
    except Exception as e:
        add_log(f"✗ 错误: {e}")
        import traceback
        add_log(traceback.format_exc())
        traceback.print_exc()
    finally:
        _set_task_state(False)
        add_log("空闲中，可发起新任务")


HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>知乎爬虫</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    background: #0f0f0f;
    color: #e0e0e0;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 40px 20px;
  }
  h1 {
    font-size: 28px;
    font-weight: 700;
    margin-bottom: 6px;
    background: linear-gradient(135deg, #00a1d4, #00d4aa);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }
  .subtitle { color: #555; font-size: 13px; margin-bottom: 32px; }
  .container { width: 100%; max-width: 860px; display: flex; flex-direction: column; gap: 20px; }
  .card {
    background: #1a1a1a;
    border: 1px solid #2a2a2a;
    border-radius: 12px;
    padding: 24px;
  }
  .card-title {
    font-size: 11px;
    font-weight: 600;
    color: #666;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-bottom: 16px;
  }
  .tabs { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
  .tab {
    padding: 7px 16px;
    border-radius: 8px;
    border: 1px solid #2a2a2a;
    background: transparent;
    color: #666;
    font-size: 13px;
    cursor: pointer;
    transition: all 0.2s;
  }
  .tab:hover { border-color: #00a1d4; color: #00a1d4; }
  .tab.active { background: #00a1d4; border-color: #00a1d4; color: #fff; }
  .panel { display: none; }
  .panel.active { display: block; }
  .input-row { display: flex; gap: 10px; margin-bottom: 8px; align-items: center; }
  .stack { display: flex; flex-direction: column; gap: 12px; }
  .input {
    flex: 1;
    padding: 10px 14px;
    border-radius: 8px;
    border: 1px solid #2a2a2a;
    background: #111;
    color: #e0e0e0;
    font-size: 14px;
    outline: none;
    transition: border-color 0.2s;
  }
  .input:focus { border-color: #00a1d4; }
  .input::placeholder { color: #444; }
  .select {
    min-width: 170px;
    padding: 10px 14px;
    border-radius: 8px;
    border: 1px solid #2a2a2a;
    background: #111;
    color: #e0e0e0;
    font-size: 14px;
    outline: none;
  }
  .checks {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    min-width: 230px;
    padding: 8px 10px;
    border-radius: 8px;
    border: 1px solid #2a2a2a;
    background: #111;
    color: #bbb;
    font-size: 13px;
  }
  .option-row {
    display: flex;
    gap: 12px;
    align-items: stretch;
    flex-wrap: wrap;
  }
  .option-block {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 220px;
    flex: 1;
  }
  .option-label {
    font-size: 12px;
    color: #777;
    font-weight: 600;
    letter-spacing: 0.04em;
  }
  .toggle-check {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 10px 12px;
    border-radius: 8px;
    border: 1px solid #2a2a2a;
    background: #111;
    color: #bbb;
    font-size: 13px;
  }
  .checks label {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
  }
  .hint { font-size: 11px; color: #444; margin-top: 4px; }
  .hint b { color: #555; }
  .btn {
    padding: 10px 20px;
    border-radius: 8px;
    border: none;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s;
    white-space: nowrap;
  }
  .btn-primary {
    background: linear-gradient(135deg, #00a1d4, #00d4aa);
    color: #fff;
  }
  .btn-primary:hover { opacity: 0.85; }
  .btn-primary:disabled { opacity: 0.35; cursor: not-allowed; }
  .btn-ghost {
    background: transparent;
    border: 1px solid #2a2a2a;
    color: #777;
  }
  .btn-ghost:hover { border-color: #444; color: #ccc; }
  .btn-pause {
    background: transparent;
    border: 1px solid #ef5350;
    color: #ef5350;
    padding: 10px 16px;
    display: none;
  }
  .btn-pause:hover { background: #ef5350; color: #fff; }
  .btn-pause.paused { border-color: #66bb6a; color: #66bb6a; }
  .btn-pause.paused:hover { background: #66bb6a; color: #fff; }
  .quick-desc { font-size: 12px; color: #555; margin-top: 10px; line-height: 2; }
  .quick-desc b { color: #888; }
  .log-area {
    height: 340px;
    overflow-y: auto;
    background: #090909;
    border: 1px solid #1a1a1a;
    border-radius: 8px;
    padding: 14px 16px;
    font-family: "SF Mono", "Fira Code", Consolas, monospace;
    font-size: 13px;
    line-height: 1.9;
  }
  .log-line { color: #383838; white-space: pre-wrap; word-break: break-all; }
  .log-line.hl { color: #00d4aa; font-weight: 600; }
  .log-line.ok { color: #66bb6a; }
  .log-line.err { color: #ef5350; }
  .log-line.warn { color: #ffb74d; }
  .log-line.idle { color: #555; }
  .status-bar {
    display: flex;
    justify-content: space-between;
    font-size: 12px;
    color: #444;
    margin-top: 8px;
  }
  .dot {
    display: inline-block;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #444;
    margin-right: 6px;
  }
  .dot.idle { background: #4caf50; }
  .dot.run { background: #ff9800; animation: b 1.2s infinite; }
  @keyframes b { 0%,100%{opacity:1} 50%{opacity:.3} }
  .btn-stop {
    background: #ef5350;
    border: 1px solid #ef5350;
    color: #fff;
    padding: 4px 12px;
    font-size: 12px;
    border-radius: 6px;
    cursor: pointer;
    margin-left: 12px;
  }
  .btn-stop:hover { background: #d32f2f; border-color: #d32f2f; }
  @media (max-width: 900px) {
    .input-row { flex-wrap: wrap; }
    .input, .select { width: 100%; }
    .option-block { min-width: 100%; }
  }
</style>
</head>
<body>

<h1>知乎爬虫</h1>
<p class="subtitle">问题回答 · 用户主页 · 热榜</p>

<div class="container">

  <div class="card">
    <div class="card-title">发起爬取</div>
    <div class="tabs">
      <button class="tab active" data-p="question">问题回答</button>
      <button class="tab" data-p="user">用户主页</button>
      <button class="tab" data-p="quick">快捷入口</button>
    </div>

    <div id="p-question" class="panel active">
      <div class="stack">
        <div class="input-row">
          <input class="input" id="q-input" placeholder="输入问题 ID 或完整链接，如 https://www.zhihu.com/question/2023447323699683399" />
          <button class="btn btn-primary" id="btn-q" onclick="scrapeQuestion()">开始爬取</button>
          <button class="btn btn-pause" id="btn-pause-q" onclick="togglePause('q')">暂停</button>
        </div>
        <div class="option-row">
          <div class="option-block">
            <div class="option-label">导出模式</div>
            <select class="select" id="q-mode">
              <option value="full">完整内容（HTML/图片）</option>
              <option value="text">纯文字 JSON</option>
            </select>
          </div>
          <div class="option-block">
            <div class="option-label">抓取策略</div>
            <label class="toggle-check"><input type="checkbox" id="q-conservative" /> 保守模式（更慢、更稳，适合大问题）</label>
          </div>
        </div>
      </div>
      <div class="hint">支持问题 ID、问题链接、回答链接，例如 zhihu.com/question/<b>2023447323699683399</b></div>
    </div>

    <div id="p-user" class="panel">
      <div class="stack">
        <div class="input-row">
          <input class="input" id="u-input" placeholder="输入用户 token 或完整链接，如 https://www.zhihu.com/people/ming--li" />
          <button class="btn btn-primary" id="btn-u" onclick="scrapeUser()">开始爬取</button>
          <button class="btn btn-pause" id="btn-pause-u" onclick="togglePause('u')">暂停</button>
        </div>
        <div class="option-row">
          <div class="option-block">
            <div class="option-label">导出模式</div>
            <select class="select" id="u-mode">
              <option value="full">完整内容（HTML/图片）</option>
              <option value="text">纯文字 JSON</option>
            </select>
          </div>
          <div class="option-block">
            <div class="option-label">用户内容筛选</div>
            <div class="checks" id="u-types">
              <label><input type="checkbox" value="answer" checked /> 回答</label>
              <label><input type="checkbox" value="article" checked /> 文章</label>
              <label><input type="checkbox" value="pin" checked /> 想法</label>
            </div>
          </div>
          <div class="option-block">
            <div class="option-label">抓取策略</div>
            <label class="toggle-check"><input type="checkbox" id="u-conservative" /> 保守模式（更慢、更稳，适合大用户）</label>
          </div>
        </div>
      </div>
      <div class="hint">支持用户 token 或用户主页链接，例如 zhihu.com/people/<b>ming--li</b></div>
    </div>

    <div id="p-quick" class="panel">
      <div class="input-row">
        <button class="btn btn-ghost" onclick="scrape('hot-list','')">🌡 热榜</button>
        <button class="btn btn-ghost" onclick="scrape('recommend','')">📋 推荐流</button>
      </div>
      <div class="quick-desc">
        <b>热榜</b> — 知乎实时热门话题，按热度排序<br>
        <b>推荐流</b> — 个性化推荐，反映账号兴趣偏好
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">实时日志</div>
    <div class="log-area" id="log-area"></div>
    <div class="status-bar">
      <span><span class="dot idle" id="dot"></span><span id="st">空闲</span></span>
      <span id="task-info"></span>
      <span><button class="btn btn-stop" id="btn-stop" onclick="stopTask()" style="display:none">终止任务</button></span>
      <span id="lc">0 条日志</span>
    </div>
  </div>

</div>

<script>
  let isRun = false, cur = null, paused = false;
  const qInput = document.getElementById('q-input');
  const uInput = document.getElementById('u-input');
  const qMode = document.getElementById('q-mode');
  const uMode = document.getElementById('u-mode');
  const qConservative = document.getElementById('q-conservative');
  const uConservative = document.getElementById('u-conservative');
  const userTypeInputs = Array.from(document.querySelectorAll('#u-types input[type="checkbox"]'));

  document.querySelectorAll('.tab').forEach(t => {
    t.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
      document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
      t.classList.add('active');
      document.getElementById('p-' + t.dataset.p).classList.add('active');
    });
  });

  function log(text, cls) {
    const area = document.getElementById('log-area');
    const d = document.createElement('div');
    d.className = 'log-line' + (cls ? ' ' + cls : '');
    const match = text.match(new RegExp('(/[^ ]+\\\\.html)'));
    if (match) {
      const [full, filePath] = match;
      const idx = text.indexOf(full);
      d.append(document.createTextNode(text.slice(0, idx)));
      const a = document.createElement('a');
      const outputIdx = filePath.indexOf('/output/');
      a.href = outputIdx >= 0 ? filePath.slice(outputIdx) : '/file?path=' + encodeURIComponent(filePath);
      a.textContent = filePath;
      a.target = '_blank';
      a.rel = 'noreferrer';
      d.append(a);
      d.append(document.createTextNode(text.slice(idx + full.length)));
    } else {
      d.textContent = text;
    }
    area.appendChild(d);
    area.scrollTop = area.scrollHeight;
    document.getElementById('lc').textContent = area.childElementCount + ' 条日志';
  }

  function status(s, txt) {
    const dot = document.getElementById('dot');
    dot.className = 'dot ' + (s === 'idle' ? 'idle' : 'run');
    document.getElementById('st').textContent = txt;
  }

  function showPause(t, show) {
    if (!t) return;
    const main = document.getElementById('btn-' + t);
    const pause = document.getElementById('btn-pause-' + t);
    if (main) main.style.display = show ? 'none' : 'inline-block';
    if (pause) pause.style.display = show ? 'inline-block' : 'none';
    if (pause && show) {
      paused = false;
      pause.classList.remove('paused');
      pause.textContent = '暂停';
    }
  }

  function taskControlKey(cmd) {
    if (cmd === 'question') return 'q';
    if (cmd === 'user') return 'u';
    return null;
  }

  function selectedUserTypes() {
    return userTypeInputs.filter(x => x.checked).map(x => x.value);
  }

  function scrapeQuestion() {
    scrape('question', qInput.value, qMode.value, '', qConservative.checked);
  }

  function scrapeUser() {
    const types = selectedUserTypes();
    if (!types.length) {
      log('⚠ 至少选择一种用户内容类型', 'warn');
      return;
    }
    scrape('user', uInput.value, uMode.value, types.join(','), uConservative.checked);
  }

  async function scrape(cmd, arg, mode='full', types='', conservative=false) {
    if (isRun) { log('⚠ 有任务正在进行中...', 'warn'); return; }
    if (!['hot-list','recommend'].includes(cmd) && !arg.trim()) {
      log('⚠ 请输入有效的 ID', 'warn'); return;
    }
    document.getElementById('log-area').replaceChildren();
    document.getElementById('lc').textContent = '0 条日志';
    isRun = true; cur = cmd; paused = false;
    status('run', '爬取中...');
    const t = taskControlKey(cmd);
    showPause(t, true);
    document.getElementById('btn-stop').style.display = 'inline-block';
    const taskNames = {question: '问题回答', user: '用户主页', 'hot-list': '热榜', recommend: '推荐流'};
    document.getElementById('task-info').textContent = taskNames[cmd] || cmd;
    const typeText = types ? ' {' + types + '}' : '';
    const paceText = conservative ? ' [safe]' : '';
    log('▶ 提交任务: ' + cmd + (arg ? ' ' + arg : '') + ' [' + mode + ']' + typeText + paceText, 'hl');
    try {
      const r = await fetch('/scrape?' + new URLSearchParams({cmd, arg, mode, types, conservative: conservative ? '1' : '0'}), {method:'POST'});
      const j = await r.json();
      if (j.status !== 'started') log('✗ ' + j.error, 'err');
    } catch(e) {
      log('✗ 请求失败: ' + e.message, 'err');
      isRun = false; cur = null; status('idle', '空闲'); showPause(t, false);
      document.getElementById('btn-stop').style.display = 'none';
      document.getElementById('task-info').textContent = '';
    }
  }

  async function togglePause(t) {
    if (!isRun) return;
    try {
      const act = paused ? '/resume' : '/pause';
      await fetch(act, {method:'POST'});
      paused = !paused;
      const btn = document.getElementById('btn-pause-' + t);
      if (paused) { btn.classList.add('paused'); btn.textContent = '继续'; log('⏸ 已暂停', 'warn'); }
      else { btn.classList.remove('paused'); btn.textContent = '暂停'; log('▶ 继续执行', 'hl'); }
    } catch(e) {}
  }

  async function stopTask() {
    if (!isRun) return;
    try {
      await fetch('/stop', {method:'POST'});
      log('⚠ 正在停止当前任务...', 'warn');
    } catch(e) {}
  }

  const es = new EventSource('/logs');
  es.onmessage = e => {
    const text = e.data;
    let cls = '';
    if (text.includes('✓')) cls = 'ok';
    else if (text.includes('✗') || text.includes('错误')) cls = 'err';
    else if (text.includes('▶')) cls = 'hl';
    else if (text.includes('⚠')) cls = 'warn';
    else if (text.includes('空闲')) cls = 'idle';
    log(text, cls);
  };

  setInterval(async () => {
    try {
      const r = await fetch('/status');
      const s = await r.json();
      if (s.running) {
        isRun = true;
        cur = s.task || cur;
        status('run', '爬取中...');
        document.getElementById('btn-stop').style.display = 'inline-block';
        const taskNames = {question: '问题回答', user: '用户主页', 'hot-list': '热榜', recommend: '推荐流'};
        document.getElementById('task-info').textContent = taskNames[s.task] || s.task || '';
      } else {
        if (isRun) {
          isRun = false; cur = null; status('idle', '空闲');
          document.getElementById('btn-stop').style.display = 'none';
          document.getElementById('task-info').textContent = '';
          ['q','u'].forEach(t => showPause(t, false));
        }
      }
    } catch(e) {}
  }, 800);
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
    print(f"  按 Ctrl+C 停止")
    print(f"=" * 40)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
