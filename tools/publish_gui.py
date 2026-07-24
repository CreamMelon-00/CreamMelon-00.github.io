# -*- coding: utf-8 -*-
"""스토리 배포 도구 (GUI)

script/ 폴더의 대본 md를 골라 storypack에 반영하고, 커밋 메시지를 입력해
커밋/푸시까지 한 번에 실행한다. 배포.bat의 GUI 확장판.

실행: 배포도구.bat 더블클릭 (또는 python tools/publish_gui.py)
"""
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = ROOT / "script"
INJECT = ROOT / "tools" / "inject_episode.py"
DEFAULT_MSG = "story content update"

# 스팀펑크 톤 (사이트와 맞춤)
BG = "#16110b"
SURFACE = "#241b12"
LINE = "#4d3b26"
TEXT = "#f0e6d2"
MUTED = "#b0a086"
ACCENT = "#cf9f4e"


def episode_sort_key(path):
    stem = path.stem
    num = stem.split("편")[0]
    try:
        return (0, int(num), stem)
    except ValueError:
        return (1, 0, stem)


class PublishApp:
    def __init__(self, root):
        self.root = root
        root.title("스토리 배포 도구")
        root.geometry("720x640")
        root.configure(bg=BG)
        self.log_queue = queue.Queue()
        self.running = False
        self.file_vars = []   # (BooleanVar, Path)

        self._build_ui()
        self.refresh_files()
        self._poll_log()

    # ---------- UI ----------
    def _build_ui(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, fieldbackground=SURFACE)
        style.configure("TCheckbutton", background=BG, foreground=TEXT)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("TButton", background=SURFACE, foreground=TEXT,
                        bordercolor=LINE, focuscolor=BG, padding=6)
        style.map("TButton", background=[("active", "#34281c")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#1c1408")
        style.map("Accent.TButton", background=[("active", "#e0b463")])
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("TEntry", insertcolor=TEXT)

        pad = {"padx": 14}

        title = ttk.Label(self.root, text="스토리 배포 도구", font=("Malgun Gothic", 15, "bold"))
        title.pack(anchor="w", pady=(12, 0), **pad)
        ttk.Label(self.root, text="반영할 대본을 고르고, 커밋 메시지를 적고, 배포를 누르세요.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 8), **pad)

        # 파일 목록 (체크박스 + 스크롤)
        list_frame = tk.Frame(self.root, bg=SURFACE, highlightbackground=LINE,
                              highlightthickness=1)
        list_frame.pack(fill="both", expand=True, **pad)

        canvas = tk.Canvas(list_frame, bg=SURFACE, highlightthickness=0, height=220)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        self.check_frame = tk.Frame(canvas, bg=SURFACE)
        self.check_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.check_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        scrollbar.pack(side="right", fill="y")

        # 목록 버튼 줄
        btn_row = tk.Frame(self.root, bg=BG)
        btn_row.pack(fill="x", pady=(6, 0), **pad)
        ttk.Button(btn_row, text="새로고침", command=self.refresh_files).pack(side="left")
        ttk.Button(btn_row, text="전체 선택", command=lambda: self._set_all(True)).pack(side="left", padx=(6, 0))
        ttk.Button(btn_row, text="전체 해제", command=lambda: self._set_all(False)).pack(side="left", padx=(6, 0))
        ttk.Button(btn_row, text="외부 md 추가…", command=self.add_external).pack(side="left", padx=(6, 0))

        # 커밋 메시지
        msg_row = tk.Frame(self.root, bg=BG)
        msg_row.pack(fill="x", pady=(12, 0), **pad)
        ttk.Label(msg_row, text="커밋 메시지").pack(side="left")
        self.msg_var = tk.StringVar(value=DEFAULT_MSG)
        entry = tk.Entry(msg_row, textvariable=self.msg_var, bg=SURFACE, fg=TEXT,
                         insertbackground=TEXT, relief="flat",
                         highlightbackground=LINE, highlightthickness=1)
        entry.pack(side="left", fill="x", expand=True, padx=(10, 0), ipady=5)

        # 옵션 + 실행
        run_row = tk.Frame(self.root, bg=BG)
        run_row.pack(fill="x", pady=(10, 0), **pad)
        self.push_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(run_row, text="푸시까지 실행 (끄면 커밋만)",
                        variable=self.push_var).pack(side="left")
        self.run_btn = ttk.Button(run_row, text="배포 실행", style="Accent.TButton",
                                  command=self.run_publish)
        self.run_btn.pack(side="right")
        self.dry_btn = ttk.Button(run_row, text="미리 확인 (dry-run)",
                                  command=lambda: self.run_publish(dry=True))
        self.dry_btn.pack(side="right", padx=(0, 8))

        # 로그
        self.log = tk.Text(self.root, height=12, bg="#100b06", fg=MUTED,
                           relief="flat", highlightbackground=LINE, highlightthickness=1,
                           font=("Consolas", 9), state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, pady=(10, 12), **pad)

    # ---------- 파일 목록 ----------
    def refresh_files(self):
        for child in self.check_frame.winfo_children():
            child.destroy()
        self.file_vars = []
        if not SCRIPT_DIR.exists():
            self._append_log(f"script 폴더가 없습니다: {SCRIPT_DIR}\n")
            return
        files = sorted(SCRIPT_DIR.rglob("*.md"),
                       key=lambda p: (str(p.parent), episode_sort_key(p)))
        for path in files:
            self._add_file_row(path, checked=True)

    def _add_file_row(self, path, checked=True, external=False):
        var = tk.BooleanVar(value=checked)
        if external:
            label = path.name + "   (외부 파일)"
        else:
            try:
                label = str(path.relative_to(SCRIPT_DIR))
            except ValueError:
                label = path.name
        cb = tk.Checkbutton(self.check_frame, text=label, variable=var,
                            bg=SURFACE, fg=TEXT, selectcolor=BG,
                            activebackground=SURFACE, activeforeground=TEXT,
                            anchor="w", font=("Malgun Gothic", 10))
        cb.pack(fill="x", anchor="w", padx=4)
        self.file_vars.append((var, path))

    def _set_all(self, value):
        for var, _ in self.file_vars:
            var.set(value)

    def add_external(self):
        paths = filedialog.askopenfilenames(
            title="반영할 md 파일 선택", filetypes=[("Markdown", "*.md")])
        known = {p for _, p in self.file_vars}
        for p in paths:
            path = Path(p)
            if path not in known:
                self._add_file_row(path, checked=True, external=True)

    # ---------- 실행 ----------
    def run_publish(self, dry=False):
        if self.running:
            return
        selected = [p for var, p in self.file_vars if var.get()]
        if not selected:
            messagebox.showwarning("선택 없음", "반영할 md 파일을 하나 이상 선택하세요.")
            return
        msg = self.msg_var.get().strip() or DEFAULT_MSG
        self.running = True
        self.run_btn.state(["disabled"])
        self.dry_btn.state(["disabled"])
        self._clear_log()
        threading.Thread(target=self._pipeline, args=(selected, msg, dry),
                         daemon=True).start()

    def _pipeline(self, files, msg, dry):
        try:
            total = len(files)
            for i, path in enumerate(files, 1):
                self.log_queue.put(f"[{i}/{total}] {path.name}\n")
                cmd = [sys.executable, str(INJECT), str(path)]
                if dry:
                    cmd.append("--dry-run")
                if not self._run(cmd):
                    self.log_queue.put("\n중단: 반영 중 오류가 발생했습니다.\n")
                    return
            if dry:
                self.log_queue.put("\ndry-run 완료 — 저장/커밋하지 않았습니다.\n")
                return

            self.log_queue.put("\n[git] 변경 사항 커밋...\n")
            self._run(["git", "add", "data", "resource", "script"], cwd=ROOT)
            committed = self._run(["git", "commit", "-m", msg], cwd=ROOT)
            if not committed:
                self.log_queue.put("(커밋할 변경이 없거나 커밋 실패 — 위 메시지 확인)\n")

            if self.push_var.get():
                self.log_queue.put("\n[git] 푸시...\n")
                if self._run(["git", "push"], cwd=ROOT):
                    self.log_queue.put("\n완료! 사이트는 1~3분 안에 갱신됩니다.\n")
                else:
                    self.log_queue.put("\n푸시 실패 — 네트워크/인증을 확인하세요.\n")
            else:
                self.log_queue.put("\n커밋까지 완료 (푸시는 하지 않음).\n")
        finally:
            self.log_queue.put(("__done__",))

    def _run(self, cmd, cwd=None):
        try:
            proc = subprocess.run(
                cmd, cwd=cwd or ROOT, capture_output=True,
                encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except FileNotFoundError as e:
            self.log_queue.put(f"실행 실패: {e}\n")
            return False
        if proc.stdout:
            self.log_queue.put(proc.stdout)
        if proc.stderr:
            self.log_queue.put(proc.stderr)
        return proc.returncode == 0

    # ---------- 로그 ----------
    def _poll_log(self):
        try:
            while True:
                item = self.log_queue.get_nowait()
                if isinstance(item, tuple) and item[0] == "__done__":
                    self.running = False
                    self.run_btn.state(["!disabled"])
                    self.dry_btn.state(["!disabled"])
                else:
                    self._append_log(item)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_log)

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")


if __name__ == "__main__":
    root = tk.Tk()
    PublishApp(root)
    root.mainloop()
