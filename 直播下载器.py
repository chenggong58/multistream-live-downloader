# -*- coding: utf-8 -*-
"""
多平台直播下载器（统一入口版，子进程强杀停止方案）
支持：YouTube / Twitch / B站 / 抖音 / TikTok（国际版）
- 自动识别平台，自动下载最高画质
- 多行链接、多任务并发下载、中途加播
- 每个下载跑在独立子进程里，点「停止」用 taskkill 强杀进程树，100% 能停
"""
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import sys
import os
import subprocess
import threading
import socket
import shutil
import winsound  # Windows 系统提示音

# -------------------------- 全局配置 --------------------------
COMMON_PROXY_PORTS = [7897, 7890, 7891, 7899, 1080, 10808, 10809, 8888, 1081]
DEFAULT_PROXY = "127.0.0.1:7897"
FORMAT_BEST = "bestvideo*+bestaudio/best"  # 优先音视频分离取最高画质，直播单流自动回退

# -------------------------- 下载 worker（在独立子进程里运行） --------------------------
def worker_main(url, save, proxy):
    import yt_dlp
    opts = {
        'outtmpl': f'{save}/%(title)s [%(id)s].%(ext)s',
        'format': FORMAT_BEST,
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'retries': 10,
        'fragment_retries': 10,
        'retry_sleep': 'exp=1:20',
        'concurrent_fragments': 4,
        'hls_use_mpegts': False,
        'no_check_certificate': True,
        'quiet': True,
        'no_warnings': True,
    }
    if proxy:
        opts['proxy'] = f'http://{proxy}'

    def hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            if total:
                pct = d['downloaded_bytes'] / total * 100
                sp = d.get('speed', 0)
                sps = f"{sp/1024/1024:.2f}MB/s" if sp else "?"
                print(f"PROGRESS:{pct:.1f}:{sps}", flush=True)
            else:
                print(f"PROGRESS:MB:{d['downloaded_bytes']/1024/1024:.1f}", flush=True)
        elif d['status'] == 'finished':
            print("STATUS:merge", flush=True)

    opts['progress_hooks'] = [hook]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
        print("STATUS:done", flush=True)
    except Exception as e:
        print(f"STATUS:error:{str(e)}", flush=True)

# -------------------------- 启动自检 --------------------------
def detect_proxy_port():
    for port in COMMON_PROXY_PORTS:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.3)
            s.close()
            return f"127.0.0.1:{port}"
        except OSError:
            continue
    return None

def ffmpeg_available():
    return shutil.which("ffmpeg") is not None

def guess_platform(url):
    u = url.lower()
    if 'youtube.com' in u or 'youtu.be' in u: return "YouTube"
    if 'twitch.tv' in u:                       return "Twitch"
    if 'bilibili.com' in u:                    return "B站"
    if 'douyin.com' in u:                      return "抖音"
    if 'tiktok.com' in u:                      return "TikTok"
    return "未知/自动"

def auto_check_startup():
    def check():
        proxy = detect_proxy_port()
        has_ffmpeg = ffmpeg_available()
        def apply():
            if proxy:
                entry_proxy.delete(0, tk.END)
                entry_proxy.insert(0, proxy)
                pinfo = f"已自动检测到代理 {proxy}"
            else:
                pinfo = f"未检测到代理（默认 {DEFAULT_PROXY}，连不上请手动填Clash端口）"
            finfo = "ffmpeg正常，可自动合并" if has_ffmpeg else "⚠️ 未找到ffmpeg，可能无法合并音视频"
            label_status.config(text=f"✅ 就绪 | {pinfo} | {finfo}")
        root.after(0, apply)
    threading.Thread(target=check, daemon=True).start()

# -------------------------- 任务管理（GUI 主进程） --------------------------
tasks = {}          # tid -> {platform, url, status, progress, proc, stopped}
tid_counter = 0
added_urls = set()  # 已加入下载的链接，用于中途加播时去重

def add_task(platform, url):
    global tid_counter
    tid_counter += 1
    tid = tid_counter
    tasks[tid] = {"platform": platform, "url": url, "status": "排队中", "progress": 0, "proc": None, "stopped": False}
    tree.insert("", "end", iid=str(tid), values=(tid, platform, "排队中", "0%", url))
    return tid

def update_row(tid):
    if tree.exists(str(tid)):
        t = tasks[tid]
        tree.item(str(tid), values=(tid, t["platform"], t["status"], f"{t['progress']:.1f}%", t["url"]))

def task_status(tid, status):
    tasks[tid]["status"] = status
    root.after(0, lambda: update_row(tid))

def read_worker(tid):
    """读取子进程输出，更新进度与状态"""
    p = tasks[tid]["proc"]
    try:
        for line in p.stdout:
            line = line.strip()
            if line.startswith("PROGRESS:"):
                parts = line.split(":")
                if parts[1] == "MB":
                    tasks[tid]["status"] = f"下载中 {parts[2]}MB"
                else:
                    tasks[tid]["progress"] = float(parts[1])
                    tasks[tid]["status"] = f"下载中 {parts[1]}% {parts[2]}"
                root.after(0, lambda: update_row(tid))
            elif line.startswith("STATUS:"):
                cmd = line[len("STATUS:"):]
                if cmd == "merge":
                    tasks[tid]["status"] = "已下载，合并中..."
                elif cmd == "done":
                    tasks[tid]["progress"] = 100
                    tasks[tid]["status"] = "✅ 已完成"
                    root.after(0, notify_done)
                elif cmd.startswith("error:"):
                    err = cmd[len("error:"):]
                    tasks[tid]["status"] = "❌ 下载失败"
                    root.after(0, lambda: messagebox.showerror("下载失败", f"{tasks[tid]['url']}\n\n{err}\n\n常见原因：\n1. 链接无效或直播未开播\n2. 代理连不上目标平台\n3. 抖音/TikTok 风控需换节点"))
                root.after(0, lambda: update_row(tid))
    except Exception:
        pass

def start_download():
    global added_urls
    urls = [l.strip() for l in text_urls.get("1.0", "end").splitlines() if l.strip()]
    if not urls:
        messagebox.showwarning("提示", "请先粘贴直播链接（可多行，每行一个）")
        return
    save = entry_path.get().strip()
    if not save:
        messagebox.showwarning("提示", "请选择保存文件夹")
        return

    # 只启动本次新增的链接（已加入过的自动跳过，方便中途加播）
    new_urls = [u for u in urls if u not in added_urls]
    for u in new_urls:
        added_urls.add(u)

    if not new_urls:
        messagebox.showinfo("提示",
            "这些链接都已经在下载列表里了，没有新增。\n\n想中途加直播：直接把新链接粘进来，再点一次「开始下载」即可，正在下载的不会中断。")
        return

    for u in new_urls:
        tid = add_task(guess_platform(u), u)
        proxy = entry_proxy.get().strip() if proxy_var.get() else ""
        # 每个下载放进独立子进程，停止时可强杀
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        p = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--worker", u, save, proxy],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        tasks[tid]["proc"] = p
        threading.Thread(target=read_worker, args=(tid,), daemon=True).start()

    running = sum(1 for t in tasks.values() if t["status"] != "✅ 已完成")
    label_status.config(text=f"🚀 本次新增 {len(new_urls)} 个任务，当前共 {len(tasks)} 个（进行中 {running}）。随时粘新链接再点开始即可中途加播")

def notify_done():
    """任务下载完成时播放系统提示音"""
    try:
        winsound.PlaySound("SystemAsterisk", winsound.SND_ALIAS)
    except Exception:
        pass

def stop_selected():
    """强杀选中的任务进程树（连带 ffmpeg），100% 能停"""
    sel = tree.selection()
    if not sel:
        messagebox.showinfo("提示", "请先在任务列表里点选要停止的任务（高亮行）")
        return
    tid = int(sel[0])
    cur = tasks[tid]["status"]
    if cur in ("✅ 已完成", "⏹ 已停止", "⏹ 正在停止..."):
        messagebox.showinfo("提示", "该任务已经完成或正在停止")
        return
    p = tasks[tid].get("proc")
    if not p or p.poll() is not None:
        tasks[tid]["stopped"] = True
        task_status(tid, "⏹ 已停止")
        return
    tasks[tid]["stopped"] = True
    task_status(tid, "⏹ 正在停止...")
    # taskkill /T /F 杀整棵进程树（含 ffmpeg）
    subprocess.run(["taskkill", "/pid", str(p.pid), "/T", "/F"],
                   capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    task_status(tid, "⏹ 已停止")
    root.after(0, lambda: messagebox.showinfo("已停止",
        f"已停止下载：\n{tasks[tid]['url']}\n已下载的部分保留在保存文件夹"))

# -------------------------- 入口：区分 GUI 与 worker 子进程 --------------------------
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        # 子进程模式：只做下载，不启动界面
        worker_main(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "")
    else:
        # ============ GUI 模式 ============
        root = tk.Tk()
        root.title("多平台直播下载器（YouTube/Twitch/B站/抖音/TikTok）")
        root.geometry("780x580")
        root.resizable(False, False)

        # 1. 链接输入（多行）
        ttk.Label(root, text="📌 直播/视频链接（每行一个，自动识别平台）：", font=("微软雅黑", 10)).place(x=20, y=15)
        text_urls = tk.Text(root, width=78, height=6, font=("微软雅黑", 10))
        text_urls.place(x=20, y=45)

        # 2. 保存路径
        ttk.Label(root, text="📁 保存路径：", font=("微软雅黑", 10)).place(x=20, y=135)
        entry_path = ttk.Entry(root, width=62, font=("微软雅黑", 10))
        entry_path.place(x=130, y=135)
        ttk.Button(root, text="选择文件夹", command=lambda: entry_path.insert(0, filedialog.askdirectory())).place(x=660, y=132)

        # 3. 代理设置
        proxy_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(root, text="启用代理", variable=proxy_var).place(x=20, y=170)
        entry_proxy = ttk.Entry(root, width=20, font=("微软雅黑", 10))
        entry_proxy.place(x=100, y=170)
        entry_proxy.insert(0, DEFAULT_PROXY)
        ttk.Label(root, text="(FlClash默认7897，启动自动检测)", font=("微软雅黑", 8),
                  foreground="gray").place(x=245, y=173)

        # 4. 下载按钮
        btn = tk.Button(root, text="🚀 开始下载（最高画质·并发）", command=start_download,
                        bg="#e74c3c", fg="white", font=("微软雅黑", 11, "bold"), cursor="hand2")
        btn.place(x=300, y=200, width=220, height=45)
        btn_clear = tk.Button(root, text="🧹 清空输入框", command=lambda: text_urls.delete("1.0", "end"),
                              font=("微软雅黑", 10))
        btn_clear.place(x=540, y=200, width=110, height=45)

        # 5. 任务列表
        tree = ttk.Treeview(root, columns=("id", "平台", "状态", "进度", "链接"), show="headings", height=10)
        tree.heading("id", text="ID")
        tree.heading("平台", text="平台")
        tree.heading("状态", text="状态")
        tree.heading("进度", text="进度")
        tree.heading("链接", text="链接")
        tree.column("id", width=40, anchor="center")
        tree.column("平台", width=80, anchor="center")
        tree.column("状态", width=200, anchor="center")
        tree.column("进度", width=80, anchor="center")
        tree.column("链接", width=340)
        tree.place(x=20, y=260)

        # 6. 状态栏
        label_status = ttk.Label(root, text="就绪中...", font=("微软雅黑", 10))
        label_status.place(x=20, y=540)

        # 停止按钮（放在任务列表下方）
        btn_stop = tk.Button(root, text="⏹ 停止选中任务", command=stop_selected,
                             bg="#f39c12", fg="white", font=("微软雅黑", 10, "bold"), cursor="hand2")
        btn_stop.place(x=20, y=475, width=150, height=35)

        # 7. 版权提示
        ttk.Label(root, text="⚠️ 仅下载已授权内容，遵守各平台服务条款及法律法规",
                  foreground="red", font=("微软雅黑", 9)).place(x=20, y=560)

        # 启动自检
        auto_check_startup()

        root.mainloop()
