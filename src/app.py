"""
ThetaCode – tkinter desktop GUI
Run with:  python src/app.py
"""
import atexit
import json
import os
import sys
import queue
import threading
import time
import tkinter as tk
import typing as t
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / '.env')

# Ensure the src package is importable when running as `python app.py`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from storage import Storage                        # noqa: E402
from main import Project, ThetaCode, Chat          # noqa: E402
from llm import get_llm                            # noqa: E402
from merge import detect_changes, apply_changes, GitignoreMatcher, make_diff  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_MODEL = "openrouter/deepseek/deepseek-v4-flash"
SIDE_PANEL_WIDTH = 210

# Dark theme colours
BG_DARK = "#1e1e1e"
BG_SURFACE = "#252525"
BG_SURFACE_LOW = "#1a1a1a"
BG_SURFACE_CONTAINER = "#2d2d2d"
BG_PRIMARY_CONTAINER = "#1a3a5c"
BG_SECONDARY_CONTAINER = "#3a2a5c"
FG_PRIMARY = "#e0e0e0"
FG_VARIANT = "#a0a0a0"
FG_TERTIARY = "#7eb6ff"
ACCENT_BLUE = "#4a9eff"
DANGER_RED = "#e05555"


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
def _msg_bgcolor(role: str) -> str:
    return {
        "user": "#1a3a5c",
        "assistant": "#3a2a5c",
    }.get(role, "#2d2d2d")


def _role_label(msg: dict) -> str:
    role = msg.get("role", "")
    if role == "user":
        content = msg.get("content", "")
        if content.lstrip().startswith("<tool_response>"):
            return "Tool Result"
        return "You"
    if role == "assistant":
        return "AI (question)" if msg.get("_ask_user") else "AI"
    return role.capitalize()


def _display_content(msg: dict) -> str:
    content = msg.get("content", "")
    for tag in ("user_message", "tool_response"):
        open_tag = f"<{tag}>"
        close_tag = f"</{tag}>"
        if content.lstrip().startswith(open_tag) and close_tag in content:
            content = content.split(open_tag, 1)[-1].split(close_tag, 1)[0].strip()
    return content


# ---------------------------------------------------------------------------
# Per-chat state
# ---------------------------------------------------------------------------
class _ChatState:
    def __init__(self, chat_id: int, frame: tk.Frame, messages_text: tk.Text):
        self.chat_id = chat_id
        self.frame = frame
        self.chat: Chat | None = None
        self.messages_text = messages_text
        self.is_thinking = False
        self.stream_queue: queue.Queue = queue.Queue()
        self.cancel_event: threading.Event | None = None
        self.current_stream_placeholder: str | None = None
        self.thinking_blocks: dict = {}
        self.cost = 0.0
        self.stream_placeholders: dict = {}
        self._poll_running = False  # avoid duplicate UI poll loops


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------
class ThetaCodeApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("ThetaCode")
        self.root.configure(bg=BG_DARK)
        self.root.geometry("1200x750")
        self.root.minsize(800, 500)

        self._configure_style()
        self.storage = Storage()

        self.project_id = None
        self.active_chat_id: int | None = None
        self.theta_code = None
        self._chat_states: dict[int, _ChatState] = {}
        self._stream_token_counter = 0
        self._docker_starting = False

        self._build_ui()

        atexit.register(self._cleanup)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._refresh_projects()

    def _configure_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG_DARK, foreground=FG_PRIMARY)
        style.configure("TFrame", background=BG_DARK)
        style.configure("TLabel", background=BG_DARK, foreground=FG_PRIMARY)
        style.configure("TButton", background=BG_SURFACE_CONTAINER, foreground=FG_PRIMARY,
                        borderwidth=1, relief="flat", padding=(8, 4))
        style.map("TButton", background=[("active", "#3d3d3d"), ("pressed", "#4d4d4d")])
        style.configure("TEntry", fieldbackground=BG_SURFACE_CONTAINER, foreground=FG_PRIMARY,
                        borderwidth=1, relief="solid")
        style.configure("TProgressbar", troughcolor=BG_SURFACE_CONTAINER, background=ACCENT_BLUE)
        style.configure("TSeparator", background="#3d3d3d")

        self.root.option_add("*Listbox.background", BG_SURFACE_LOW)
        self.root.option_add("*Listbox.foreground", FG_PRIMARY)
        self.root.option_add("*Listbox.selectBackground", ACCENT_BLUE)
        self.root.option_add("*Listbox.selectForeground", "#ffffff")
        self.root.option_add("*Listbox.borderWidth", 0)
        self.root.option_add("*Listbox.highlightThickness", 0)
        self.root.option_add("*Listbox.font", ("TkDefaultFont", 11))

    def _build_ui(self):
        self._paned = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, bg="#3d3d3d",
                                     sashwidth=1, sashrelief=tk.FLAT)
        self._paned.pack(fill=tk.BOTH, expand=True)

        self._left_frame = tk.Frame(self._paned, bg=BG_SURFACE_LOW, width=SIDE_PANEL_WIDTH)
        self._paned.add(self._left_frame, minsize=150, width=SIDE_PANEL_WIDTH, stretch="never")
        self._build_projects_panel()

        self._mid_frame = tk.Frame(self._paned, bg=BG_SURFACE_LOW, width=SIDE_PANEL_WIDTH)
        self._paned.add(self._mid_frame, minsize=150, width=SIDE_PANEL_WIDTH, stretch="never")
        self._build_chats_panel()

        self._right_frame = tk.Frame(self._paned, bg=BG_SURFACE)
        self._paned.add(self._right_frame, minsize=300, stretch="always")
        self._build_chat_panel()

    def _build_projects_panel(self):
        f = self._left_frame
        header = tk.Frame(f, bg=BG_SURFACE_LOW)
        header.pack(fill=tk.X, padx=12, pady=(10, 4))
        tk.Label(header, text="Projects", bg=BG_SURFACE_LOW, fg=FG_PRIMARY,
                 font=("TkDefaultFont", 12, "bold"), anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(header, text="+", bg=BG_SURFACE_CONTAINER, fg=FG_PRIMARY,
                  relief=tk.FLAT, bd=0, font=("TkDefaultFont", 14, "bold"),
                  activebackground="#3d3d3d", activeforeground=FG_PRIMARY,
                  command=self._open_new_project_dialog, cursor="hand2").pack(side=tk.RIGHT)

        tk.Frame(f, bg="#3d3d3d", height=1).pack(fill=tk.X, padx=4)

        list_frame = tk.Frame(f, bg=BG_SURFACE_LOW)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self._projects_list = tk.Listbox(list_frame, bg=BG_SURFACE_LOW, fg=FG_PRIMARY,
                                         selectmode=tk.SINGLE, activestyle="none",
                                         relief=tk.FLAT, bd=0, highlightthickness=0,
                                         exportselection=False)
        scrollbar = tk.Scrollbar(list_frame, orient=tk.VERTICAL, bg=BG_SURFACE_CONTAINER,
                                 troughcolor=BG_DARK, activebackground="#4d4d4d")
        self._projects_list.configure(yscrollcommand=scrollbar.set)
        scrollbar.configure(command=self._projects_list.yview)
        self._projects_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._projects_list.bind("<<ListboxSelect>>", self._on_project_select)

        self._merge_project_btn = tk.Button(f, text="Merge Project", bg=BG_SURFACE_CONTAINER,
                                            fg=ACCENT_BLUE, relief=tk.FLAT, bd=0,
                                            activebackground="#3d3d5c", activeforeground=ACCENT_BLUE,
                                            font=("TkDefaultFont", 10), cursor="hand2",
                                            command=self._open_merge_dialog, state=tk.DISABLED)
        self._merge_project_btn.pack(fill=tk.X, padx=12, pady=(2, 2))

        self._delete_project_btn = tk.Button(f, text="Delete Project", bg=BG_SURFACE_CONTAINER,
                                             fg=DANGER_RED, relief=tk.FLAT, bd=0,
                                             activebackground="#4d3d3d", activeforeground=DANGER_RED,
                                             font=("TkDefaultFont", 10), cursor="hand2",
                                             command=self._confirm_delete_project, state=tk.DISABLED)
        self._delete_project_btn.pack(fill=tk.X, padx=12, pady=(2, 10))

    def _build_chats_panel(self):
        f = self._mid_frame
        header = tk.Frame(f, bg=BG_SURFACE_LOW)
        header.pack(fill=tk.X, padx=12, pady=(10, 4))
        tk.Label(header, text="Chats", bg=BG_SURFACE_LOW, fg=FG_PRIMARY,
                 font=("TkDefaultFont", 12, "bold"), anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(header, text="+", bg=BG_SURFACE_CONTAINER, fg=FG_PRIMARY,
                  relief=tk.FLAT, bd=0, font=("TkDefaultFont", 14, "bold"),
                  activebackground="#3d3d3d", activeforeground=FG_PRIMARY,
                  command=self._open_new_chat_dialog, cursor="hand2").pack(side=tk.RIGHT)

        tk.Frame(f, bg="#3d3d3d", height=1).pack(fill=tk.X, padx=4)

        list_frame = tk.Frame(f, bg=BG_SURFACE_LOW)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self._chats_list = tk.Listbox(list_frame, bg=BG_SURFACE_LOW, fg=FG_PRIMARY,
                                      selectmode=tk.SINGLE, activestyle="none",
                                      relief=tk.FLAT, bd=0, highlightthickness=0,
                                      exportselection=False)
        scrollbar = tk.Scrollbar(list_frame, orient=tk.VERTICAL, bg=BG_SURFACE_CONTAINER,
                                 troughcolor=BG_DARK, activebackground="#4d4d4d")
        self._chats_list.configure(yscrollcommand=scrollbar.set)
        scrollbar.configure(command=self._chats_list.yview)
        self._chats_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._chats_list.bind("<<ListboxSelect>>", self._on_chat_select)

        self._copy_json_btn = tk.Button(f, text="Copy as JSON", bg=BG_SURFACE_CONTAINER,
                                        fg=FG_TERTIARY, relief=tk.FLAT, bd=0,
                                        activebackground="#2d3d5c", activeforeground=FG_TERTIARY,
                                        font=("TkDefaultFont", 10), cursor="hand2",
                                        command=self._copy_chat_as_json, state=tk.DISABLED)
        self._copy_json_btn.pack(fill=tk.X, padx=12, pady=(2, 2))

        self._close_chat_btn = tk.Button(f, text="Close Chat", bg=BG_SURFACE_CONTAINER,
                                         fg=FG_TERTIARY, relief=tk.FLAT, bd=0,
                                         activebackground="#2d3d5c", activeforeground=FG_TERTIARY,
                                         font=("TkDefaultFont", 10), cursor="hand2",
                                         command=self._on_close_chat, state=tk.DISABLED)
        self._close_chat_btn.pack(fill=tk.X, padx=12, pady=(2, 2))

        self._delete_chat_btn = tk.Button(f, text="Delete Chat", bg=BG_SURFACE_CONTAINER,
                                          fg=DANGER_RED, relief=tk.FLAT, bd=0,
                                          activebackground="#4d3d3d", activeforeground=DANGER_RED,
                                          font=("TkDefaultFont", 10), cursor="hand2",
                                          command=self._confirm_delete_chat, state=tk.DISABLED)
        self._delete_chat_btn.pack(fill=tk.X, padx=12, pady=(2, 10))

    def _build_chat_panel(self):
        f = self._right_frame

        self._chat_container = tk.Frame(f, bg=BG_SURFACE)
        self._chat_container.pack(fill=tk.BOTH, expand=True)

        bottom = tk.Frame(f, bg=BG_SURFACE)
        bottom.pack(fill=tk.X, side=tk.BOTTOM)

        tk.Frame(bottom, bg="#3d3d3d", height=1).pack(fill=tk.X)

        status_row = tk.Frame(bottom, bg=BG_SURFACE)
        status_row.pack(fill=tk.X, padx=12, pady=(6, 4))

        self._thinking_label = tk.Label(status_row, text="", bg=BG_SURFACE, fg=FG_VARIANT,
                                        font=("TkDefaultFont", 11, "italic"))
        self._thinking_label.pack(side=tk.LEFT)

        self._thinking_progress = ttk.Progressbar(status_row, mode="indeterminate",
                                                    length=20, style="TProgressbar")

        status_right = tk.Frame(status_row, bg=BG_SURFACE)
        status_right.pack(side=tk.RIGHT)

        tk.Label(status_right, text="Model:", bg=BG_SURFACE, fg=FG_VARIANT,
                 font=("TkDefaultFont", 10)).pack(side=tk.LEFT, padx=(0, 4))

        self._model_var = tk.StringVar(value=DEFAULT_MODEL)
        self._model_entry = tk.Entry(status_right, textvariable=self._model_var,
                                     bg=BG_SURFACE_CONTAINER, fg=FG_PRIMARY,
                                     relief=tk.FLAT, bd=0, insertbackground=FG_PRIMARY,
                                     font=("TkDefaultFont", 10), width=30)
        self._model_entry.pack(side=tk.LEFT, padx=(0, 12))

        self._cost_label = tk.Label(status_right, text="Cost: $0.0000", bg=BG_SURFACE,
                                    fg=FG_VARIANT, font=("TkDefaultFont", 10))
        self._cost_label.pack(side=tk.LEFT)

        self._docker_label = tk.Label(status_row, text="", bg=BG_SURFACE, fg=FG_VARIANT,
                                      font=("TkDefaultFont", 10, "italic"))
        self._docker_label.pack(side=tk.LEFT, padx=(12, 0))

        input_row = tk.Frame(bottom, bg=BG_SURFACE)
        input_row.pack(fill=tk.X, padx=12, pady=(4, 12))

        self._input_text = tk.Text(input_row, bg=BG_SURFACE_CONTAINER, fg=FG_PRIMARY,
                                   relief=tk.FLAT, bd=0, insertbackground=FG_PRIMARY,
                                   font=("TkDefaultFont", 12), height=2, wrap=tk.WORD,
                                   padx=10, pady=8)
        self._input_text.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self._input_text.bind("<Return>", self._on_input_return)
        self._input_text.bind("<Shift-Return>", self._on_input_shift_return)
        self._input_text.configure(state=tk.DISABLED)

        self._send_btn = tk.Button(input_row, text="Send", bg=ACCENT_BLUE, fg="#ffffff",
                                   relief=tk.FLAT, bd=0, font=("TkDefaultFont", 11, "bold"),
                                   activebackground="#5ab8ff", activeforeground="#ffffff",
                                   command=self._do_send, cursor="hand2",
                                   state=tk.DISABLED, padx=16, pady=4)
        self._send_btn.pack(side=tk.RIGHT)

        self._cancel_btn = tk.Button(input_row, text="Cancel", bg="#bf7b00", fg="#ffffff",
                                     relief=tk.FLAT, bd=0, font=("TkDefaultFont", 11, "bold"),
                                     activebackground="#d49500", activeforeground="#ffffff",
                                     command=self._do_cancel, cursor="hand2",
                                     state=tk.DISABLED, padx=16, pady=4)

    def _create_chat_widget(self) -> tuple[tk.Frame, tk.Text]:
        frame = tk.Frame(self._chat_container, bg=BG_SURFACE)
        text_widget = tk.Text(
            frame, bg=BG_SURFACE, fg=FG_PRIMARY,
            wrap=tk.WORD, state=tk.DISABLED,
            relief=tk.FLAT, bd=0, highlightthickness=0,
            padx=12, pady=12, spacing1=4, spacing2=4, spacing3=0,
            font=("TkDefaultFont", 12),
        )
        msg_scrollbar = tk.Scrollbar(frame, orient=tk.VERTICAL, bg=BG_SURFACE_CONTAINER,
                                     troughcolor=BG_DARK, activebackground="#4d4d4d")
        text_widget.configure(yscrollcommand=msg_scrollbar.set)
        msg_scrollbar.configure(command=text_widget.yview)
        text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        msg_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Tags
        text_widget.tag_configure("bubble_user", background=BG_PRIMARY_CONTAINER,
                                  lmargin1=10, lmargin2=10, rmargin=10,
                                  spacing1=4, spacing3=4, wrap=tk.WORD)
        text_widget.tag_configure("bubble_ai", background=BG_SECONDARY_CONTAINER,
                                  lmargin1=10, lmargin2=10, rmargin=10,
                                  spacing1=4, spacing3=4, wrap=tk.WORD)
        text_widget.tag_configure("bubble_tool", background=BG_SURFACE_CONTAINER,
                                  lmargin1=10, lmargin2=10, rmargin=10,
                                  spacing1=4, spacing3=4, wrap=tk.WORD)
        text_widget.tag_configure("bubble_error", background="#5c2a2a",
                                  lmargin1=10, lmargin2=10, rmargin=10,
                                  spacing1=4, spacing3=4, wrap=tk.WORD)
        text_widget.tag_configure("role_label", foreground=FG_VARIANT,
                                  font=("TkDefaultFont", 10, "bold"))
        text_widget.tag_configure("cost_label", foreground=FG_VARIANT,
                                  font=("TkDefaultFont", 9))
        text_widget.tag_configure("thinking_header", foreground=FG_TERTIARY,
                                  font=("TkDefaultFont", 10, "italic"))
        text_widget.tag_configure("thinking_body", foreground=FG_VARIANT,
                                  font=("TkDefaultFont", 10))
        text_widget.tag_configure("content", foreground=FG_PRIMARY,
                                  font=("TkDefaultFont", 12))
        text_widget.tag_configure("error_text", foreground="#ff6b6b",
                                  font=("TkDefaultFont", 12))
        text_widget.tag_configure("sel", background="#4a9eff", foreground="#ffffff")
        text_widget.tag_raise("sel")

        return frame, text_widget

    def _cleanup(self):
        tc = self.theta_code
        if tc:
            try:
                tc.stop_docker()
            except Exception:
                pass

    def _on_close(self):
        if hasattr(self, '_shutting_down') and self._shutting_down:
            return
        self._shutting_down = True
        self._paned.pack_forget()
        self._shutdown_overlay = tk.Frame(self.root, bg=BG_DARK)
        self._shutdown_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        tk.Label(self._shutdown_overlay, bg=BG_DARK, fg=FG_PRIMARY,
                 text="Shutting down ThetaCode…", font=("TkDefaultFont", 18, "bold")).pack(expand=True, pady=(0, 12))
        progress = ttk.Progressbar(self._shutdown_overlay, mode="indeterminate", length=200, style="TProgressbar")
        progress.pack(expand=True)
        progress.start(10)
        cleanup_done = [False]
        def _do_cleanup():
            self._cleanup()
            cleanup_done[0] = True
        threading.Thread(target=_do_cleanup, daemon=True).start()
        start_time = [time.time()]
        def _poll_shutdown():
            if cleanup_done[0] or time.time() - start_time[0] > 5.0:
                progress.stop()
                self.root.destroy()
            else:
                self.root.after(100, _poll_shutdown)
        self.root.after(100, _poll_shutdown)

    # -----------------------------------------------------------------------
    # Message display helpers
    # -----------------------------------------------------------------------
    def _populate_listbox(self, listbox: tk.Listbox, items: list, data_key: str):
        listbox.delete(0, tk.END)
        listbox._cached_data = {}
        for i, item in enumerate(items):
            listbox.insert(tk.END, item["name"])
            listbox._cached_data[i] = item

    def _get_selected_listbox_data(self, listbox: tk.Listbox) -> dict | None:
        selection = listbox.curselection()
        if not selection:
            return None
        idx = selection[0]
        data = getattr(listbox, "_cached_data", {})
        return data.get(idx)

    def _get_active_chat_state(self) -> _ChatState | None:
        if self.active_chat_id is None:
            return None
        return self._chat_states.get(self.active_chat_id)

    def _add_message_bubble(self, msg: dict, chat_state: _ChatState | None = None):
        if chat_state is None:
            chat_state = self._get_active_chat_state()
            if not chat_state:
                return
        text_widget = chat_state.messages_text
        role = msg.get("role", "")
        if role == "system":
            return
        text_widget.configure(state=tk.NORMAL)
        label = _role_label(msg)
        content = _display_content(msg)
        thinking = msg.get("thinking", "") or ""
        cost = msg.get("cost", 0.0) or 0.0
        is_error = "[Runtime error]" in content or "[Configuration error]" in content
        if is_error:
            bubble_tag = "bubble_error"
        elif role == "user":
            if content.lstrip().startswith("<tool_response>"):
                bubble_tag = "bubble_tool"
            else:
                bubble_tag = "bubble_user"
        else:
            bubble_tag = "bubble_ai"
        text_widget.insert(tk.END, f"{label}\n", (bubble_tag, "role_label"))
        if thinking:
            thinking_id = f"thinking_{id(msg)}_{len(chat_state.thinking_blocks)}"
            unique_header_tag = f"th_{thinking_id}"
            unique_body_tag = f"tb_{thinking_id}"
            text_widget.tag_configure(unique_header_tag, foreground="#7eb6ff",
                                      font=("TkDefaultFont", 10, "italic"))
            text_widget.tag_configure(unique_body_tag, foreground="#a0a0a0",
                                      font=("TkDefaultFont", 10))
            text_widget.insert(tk.END, "🧠 Show/hide thinking\n", (bubble_tag, unique_header_tag))
            ths = text_widget.index(tk.END + "-1c")
            text_widget.insert(tk.END, f"{thinking}\n", (bubble_tag, unique_body_tag))
            the = text_widget.index(tk.END + "-1c")
            saved_thinking = text_widget.get(ths, the)
            chat_state.thinking_blocks[thinking_id] = {
                "start": ths, "end": the, "visible": True,
                "body_tag": unique_body_tag, "header_tag": unique_header_tag,
                "bubble_tag": bubble_tag, "saved_content": saved_thinking,
            }
            text_widget.tag_bind(unique_header_tag, "<Button-1>",
                                 lambda e, bid=thinking_id, st=chat_state: self._toggle_thinking(bid, st))
        text_widget.insert(tk.END, f"{content}\n", (bubble_tag, "content"))
        if cost > 0:
            text_widget.insert(tk.END, f"${cost:.6f}\n", (bubble_tag, "cost_label"))
        text_widget.insert(tk.END, "\n")
        text_widget.configure(state=tk.DISABLED)
        text_widget.see(tk.END)

    def _insert_streaming_placeholder(self, chat_state: _ChatState | None = None) -> str:
        if chat_state is None:
            chat_state = self._get_active_chat_state()
            if not chat_state:
                return ""
        text_widget = chat_state.messages_text
        text_widget.configure(state=tk.NORMAL)
        text_widget.insert(tk.END, "AI\n", ("bubble_ai", "role_label"))
        content_start = text_widget.index(tk.END)
        text_widget.insert(tk.END, "\n", ("bubble_ai", "content"))
        content_end = text_widget.index(tk.END + "-1c")
        text_widget.configure(state=tk.DISABLED)
        text_widget.see(tk.END)
        self._stream_token_counter += 1
        placeholder_id = f"_stream_{self._stream_token_counter}"
        chat_state.stream_placeholders[placeholder_id] = {
            "content_start": content_start, "content_end": content_end,
            "tokens": [], "finalized": False,
        }
        return placeholder_id

    def _append_streaming_token(self, placeholder_id: str, token: str, chat_state: _ChatState | None = None):
        if chat_state is None:
            chat_state = self._get_active_chat_state()
            if not chat_state:
                return
        info = chat_state.stream_placeholders.get(placeholder_id)
        if not info or info.get("finalized"):
            return
        info["tokens"].append(token)
        text_widget = chat_state.messages_text
        text_widget.configure(state=tk.NORMAL)
        text_widget.delete(info["content_start"], info["content_end"])
        full_text = "".join(info["tokens"])
        text_widget.insert(info["content_start"], f"{full_text}\n", ("bubble_ai", "content"))
        info["content_end"] = text_widget.index(tk.END + "-1c")
        text_widget.configure(state=tk.DISABLED)
        text_widget.see(tk.END)

    def _finalize_streaming_bubble(self, placeholder_id: str, final_msg: dict | None,
                                   error_msg: str | None = None,
                                   chat_state: _ChatState | None = None):
        if chat_state is None:
            chat_state = self._get_active_chat_state()
            if not chat_state:
                return
        info = chat_state.stream_placeholders.get(placeholder_id)
        if not info:
            return
        info["finalized"] = True
        text_widget = chat_state.messages_text
        text_widget.configure(state=tk.NORMAL)
        text_widget.delete(info["content_start"], info["content_end"])
        mark = f"_fm_{placeholder_id}"
        text_widget.mark_set(mark, info["content_start"])
        if error_msg:
            text_widget.insert(mark, f"[Runtime error] {error_msg}\n", ("bubble_ai", "error_text"))
            text_widget.insert(mark, "\n", ("bubble_ai", "content"))
            if self.active_chat_id == chat_state.chat_id:
                self._cost_label.configure(text="Cost: $0.0000")
        elif final_msg:
            fc = final_msg.get("content", "")
            thinking = final_msg.get("thinking", "") or ""
            cost_val = final_msg.get("cost", 0.0) or 0.0
            if thinking:
                tid = f"tf_{placeholder_id}"
                uht = f"th_{tid}"
                ubt = f"tb_{tid}"
                text_widget.tag_configure(uht, foreground="#7eb6ff", font=("TkDefaultFont", 10, "italic"))
                text_widget.tag_configure(ubt, foreground="#a0a0a0", font=("TkDefaultFont", 10))
                text_widget.insert(mark, "🧠 Show/hide thinking\n", ("bubble_ai", uht))
                ths = text_widget.index(mark)
                text_widget.insert(mark, f"{thinking}\n", ("bubble_ai", ubt))
                the = text_widget.index(mark)
                st = text_widget.get(ths, the)
                chat_state.thinking_blocks[tid] = {
                    "start": ths, "end": the, "visible": True,
                    "body_tag": ubt, "header_tag": uht, "bubble_tag": "bubble_ai",
                    "saved_content": st,
                }
                text_widget.tag_bind(uht, "<Button-1>", lambda e, bid=tid, st2=chat_state: self._toggle_thinking(bid, st2))
                text_widget.insert(mark, f"{fc}\n", ("bubble_ai", "content"))
            else:
                text_widget.insert(mark, f"{fc}\n", ("bubble_ai", "content"))
            if cost_val > 0:
                text_widget.insert(mark, f"${cost_val:.6f}\n", ("bubble_ai", "cost_label"))
            text_widget.insert(mark, "\n", ("bubble_ai", "content"))
        try:
            text_widget.mark_unset(mark)
        except tk.TclError:
            pass
        text_widget.configure(state=tk.DISABLED)
        text_widget.see(tk.END)
        if placeholder_id in chat_state.stream_placeholders:
            del chat_state.stream_placeholders[placeholder_id]

    def _toggle_thinking(self, thinking_id: str, chat_state: _ChatState):
        info = chat_state.thinking_blocks.get(thinking_id)
        if not info:
            return
        text_widget = chat_state.messages_text
        text_widget.configure(state=tk.NORMAL)
        if info["visible"]:
            try:
                ct = text_widget.get(info["start"], info["end"])
                if ct.strip():
                    info["saved_content"] = ct
            except tk.TclError:
                pass
            text_widget.delete(info["start"], info["end"])
            info["visible"] = False
        else:
            bt = info.get("bubble_tag", "bubble_ai")
            bd = info.get("body_tag", "thinking_body")
            sc = info.get("saved_content", "")
            if sc:
                text_widget.insert(info["start"], sc, (bt, bd))
                ne = text_widget.index(f"{info['start']}+{len(sc.split(chr(10)))-1}l lineend")
                info["end"] = ne
            info["visible"] = True
        text_widget.configure(state=tk.DISABLED)

    def _clear_messages(self, chat_state: _ChatState):
        text_widget = chat_state.messages_text
        text_widget.configure(state=tk.NORMAL)
        text_widget.delete("1.0", tk.END)
        text_widget.configure(state=tk.DISABLED)
        chat_state.thinking_blocks.clear()

    # -----------------------------------------------------------------------
    # Project list operations
    # -----------------------------------------------------------------------
    def _refresh_projects(self):
        items = self.storage.get_projects()
        self._populate_listbox(self._projects_list, items, "id")
        if self.project_id is not None:
            for i, item in enumerate(items):
                if item["id"] == self.project_id:
                    self._projects_list.select_set(i)
                    self._projects_list.see(i)
                    break

    def _on_project_select(self, event=None):
        data = self._get_selected_listbox_data(self._projects_list)
        if not data:
            return
        pid = data["id"]
        if self.project_id == pid:
            return
        for cid in list(self._chat_states.keys()):
            self._close_chat(cid)
        old_tc = self.theta_code
        if old_tc:
            threading.Thread(target=old_tc.stop_docker, daemon=True).start()
            self.theta_code = None
        self.project_id = pid
        self.active_chat_id = None
        self._cost_label.configure(text="Cost: $0.0000")
        self._docker_label.configure(text="")
        self._disable_input()
        self._delete_project_btn.configure(state=tk.NORMAL)
        self._merge_project_btn.configure(state=tk.NORMAL)
        self._refresh_projects()
        self._refresh_chats()

    def _confirm_delete_project(self):
        if self.project_id is None:
            return
        proj_data = self._get_selected_listbox_data(self._projects_list)
        name = proj_data["name"] if proj_data else "this project"
        if messagebox.askyesno("Delete project?", f"Delete '{name}'?\n\nAll chats and messages will be permanently deleted.", parent=self.root, icon="warning"):
            if proj_data and self.project_id == proj_data["id"]:
                for cid in list(self._chat_states.keys()):
                    self._close_chat(cid)
                old_tc = self.theta_code
                if old_tc:
                    threading.Thread(target=old_tc.stop_docker, daemon=True).start()
                    self.theta_code = None
                self.project_id = None
                self.active_chat_id = None
                self._cost_label.configure(text="Cost: $0.0000")
                self._docker_label.configure(text="")
                self._disable_input()
                self._refresh_chats()
            if proj_data:
                self.storage.delete_project(proj_data["id"])
            self._refresh_projects()
            self._delete_project_btn.configure(state=tk.DISABLED)
            self._merge_project_btn.configure(state=tk.DISABLED)

    def _open_new_project_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("New Project")
        dialog.configure(bg=BG_SURFACE_CONTAINER)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.geometry("+%d+%d" % (self.root.winfo_rootx() + self.root.winfo_width() // 2 - 220, self.root.winfo_rooty() + self.root.winfo_height() // 2 - 80))
        frame = tk.Frame(dialog, bg=BG_SURFACE_CONTAINER, padx=20, pady=20)
        frame.pack()
        tk.Label(frame, text="Project Name", bg=BG_SURFACE_CONTAINER, fg=FG_PRIMARY, font=("TkDefaultFont", 10, "bold"), anchor="w").pack(fill=tk.X)
        name_var = tk.StringVar()
        name_entry = tk.Entry(frame, textvariable=name_var, bg=BG_SURFACE, fg=FG_PRIMARY, relief=tk.FLAT, bd=0, insertbackground=FG_PRIMARY, font=("TkDefaultFont", 11), width=40)
        name_entry.pack(fill=tk.X, pady=(4, 12))
        name_entry.focus_set()
        tk.Label(frame, text="Project Path", bg=BG_SURFACE_CONTAINER, fg=FG_PRIMARY, font=("TkDefaultFont", 10, "bold"), anchor="w").pack(fill=tk.X)
        path_frame = tk.Frame(frame, bg=BG_SURFACE_CONTAINER)
        path_frame.pack(fill=tk.X, pady=(4, 16))
        path_var = tk.StringVar()
        path_entry = tk.Entry(path_frame, textvariable=path_var, bg=BG_SURFACE, fg=FG_PRIMARY, relief=tk.FLAT, bd=0, insertbackground=FG_PRIMARY, font=("TkDefaultFont", 11), width=32)
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        def browse():
            p = filedialog.askdirectory(title="Select project folder", parent=dialog)
            if p:
                path_var.set(p)
        tk.Button(path_frame, text="📁", bg=BG_SURFACE_CONTAINER, fg=FG_PRIMARY, relief=tk.FLAT, bd=0, font=("TkDefaultFont", 14), activebackground="#3d3d3d", activeforeground=FG_PRIMARY, command=browse, cursor="hand2").pack(side=tk.RIGHT, padx=(4, 0))
        error_label = tk.Label(frame, text="", bg=BG_SURFACE_CONTAINER, fg=DANGER_RED, font=("TkDefaultFont", 10))
        btn_frame = tk.Frame(frame, bg=BG_SURFACE_CONTAINER)
        btn_frame.pack(fill=tk.X)
        def create():
            name = name_var.get().strip()
            path = path_var.get().strip()
            has_error = False
            if not name:
                error_label.configure(text="Name is required")
                error_label.pack(fill=tk.X, pady=(0, 8))
                has_error = True
            if not path:
                error_label.configure(text="Path is required" if not name else error_label.cget("text"))
                if not has_error:
                    error_label.pack(fill=tk.X, pady=(0, 8))
                has_error = True
            elif not Path(path).exists():
                error_label.configure(text="Path does not exist")
                error_label.pack(fill=tk.X, pady=(0, 8))
                has_error = True
            if has_error:
                return
            try:
                proj = Project.create(name, path)
                pid = self.storage.create_project(name, path)
                self.storage.update_project_paths(pid, proj.path, proj.original_path)
            except Exception as exc:
                error_label.configure(text=str(exc))
                error_label.pack(fill=tk.X, pady=(0, 8))
                return
            dialog.destroy()
            self._refresh_projects()
            self._select_project_by_id(pid)
        tk.Button(btn_frame, text="Cancel", bg=BG_SURFACE, fg=FG_PRIMARY, relief=tk.FLAT, bd=0, font=("TkDefaultFont", 10), activebackground="#3d3d3d", activeforeground=FG_PRIMARY, command=dialog.destroy, cursor="hand2").pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(btn_frame, text="Create", bg=ACCENT_BLUE, fg="#ffffff", relief=tk.FLAT, bd=0, font=("TkDefaultFont", 10, "bold"), activebackground="#5ab8ff", activeforeground="#ffffff", command=create, cursor="hand2").pack(side=tk.RIGHT)
        dialog.bind("<Return>", lambda e: create())

    def _select_project_by_id(self, pid: int):
        items = self.storage.get_projects()
        for i, item in enumerate(items):
            if item["id"] == pid:
                self._projects_list.select_clear(0, tk.END)
                self._projects_list.select_set(i)
                self._projects_list.see(i)
                self._on_project_select()
                break

    # -----------------------------------------------------------------------
    # Chat list operations
    # -----------------------------------------------------------------------
    def _refresh_chats(self):
        self._chats_list.delete(0, tk.END)
        self._delete_chat_btn.configure(state=tk.DISABLED)
        self._copy_json_btn.configure(state=tk.DISABLED)
        self._close_chat_btn.configure(state=tk.DISABLED)
        if self.project_id is None:
            return
        items = self.storage.get_chats(self.project_id)
        self._chats_list._cached_data = {}
        for i, item in enumerate(items):
            name = item["name"]
            if item["id"] in self._chat_states:
                name = f"● {name}"
            self._chats_list.insert(tk.END, name)
            self._chats_list._cached_data[i] = item
        if self.active_chat_id is not None:
            for i, item in enumerate(items):
                if item["id"] == self.active_chat_id:
                    self._chats_list.select_set(i)
                    self._chats_list.see(i)
                    break
        selected = self._get_selected_listbox_data(self._chats_list)
        if selected:
            cid = selected["id"]
            self._delete_chat_btn.configure(state=tk.NORMAL)
            self._copy_json_btn.configure(state=tk.NORMAL)
            self._close_chat_btn.configure(state=tk.NORMAL if cid in self._chat_states else tk.DISABLED)

    def _on_chat_select(self, event=None):
        data = self._get_selected_listbox_data(self._chats_list)
        if not data:
            return
        cid = data["id"]
        self._delete_chat_btn.configure(state=tk.NORMAL)
        self._copy_json_btn.configure(state=tk.NORMAL)
        self._close_chat_btn.configure(state=tk.NORMAL if cid in self._chat_states else tk.DISABLED)
        if self.active_chat_id == cid:
            return
        self._open_chat(cid)

    def _open_chat(self, chat_id: int):
        if chat_id in self._chat_states:
            self._activate_chat(chat_id)
            return
        self._ensure_docker_running()
        proj_row = self.storage.get_project(self.project_id)
        if not proj_row:
            return
        project = Project.from_path(proj_row["name"], proj_row["path"], proj_row.get("original_path"))
        stored_msgs = self.storage.get_messages(chat_id)
        frame, text_widget = self._create_chat_widget()
        state = _ChatState(chat_id, frame, text_widget)
        self._chat_states[chat_id] = state
        self._activate_chat(chat_id)
        captured_project_id = self.project_id
        def _init_chat():
            while True:
                if self.project_id != captured_project_id or chat_id not in self._chat_states:
                    return
                tc = self.theta_code
                if tc and tc._running:
                    break
                time.sleep(0.2)
            if self.project_id != captured_project_id or chat_id not in self._chat_states:
                return
            chat_obj = Chat(project, self.theta_code)
            chat_obj.restore_messages(stored_msgs)
            cs = self._chat_states[chat_id]
            cs.chat = chat_obj
            cs.cost = chat_obj.get_cost()
            def _show():
                if chat_id not in self._chat_states:
                    return
                cs = self._chat_states[chat_id]
                self._clear_messages(cs)
                for msg in stored_msgs:
                    if msg["role"] == "system":
                        continue
                    self._add_message_bubble({
                        "role": msg["role"], "content": msg["content"],
                        "thinking": msg.get("thinking", ""), "cost": msg.get("cost", 0.0),
                        "llm": msg.get("llm_model", ""),
                    }, chat_state=cs)
                if self.active_chat_id == chat_id:
                    self._sync_input_for_active_chat()
            self.root.after(0, _show)
        threading.Thread(target=_init_chat, daemon=True).start()

    def _activate_chat(self, chat_id: int):
        if self.active_chat_id == chat_id:
            return
        if self.active_chat_id is not None:
            old = self._chat_states.get(self.active_chat_id)
            if old:
                if old.is_thinking:
                    old.frame.pack_forget()
                else:
                    self._close_chat(old.chat_id)
        state = self._chat_states[chat_id]
        state.frame.pack(fill=tk.BOTH, expand=True)
        self.active_chat_id = chat_id
        self._sync_input_for_active_chat()
        self._refresh_chats()
        if (state.is_thinking or not state.stream_queue.empty()) and not state._poll_running:
            self._poll_stream_queue(chat_id)

    def _close_chat(self, chat_id: int):
        state = self._chat_states.pop(chat_id, None)
        if not state:
            return
        if state.cancel_event:
            state.cancel_event.set()
        state.frame.destroy()
        if self.active_chat_id == chat_id:
            self.active_chat_id = None
            self._sync_input_for_active_chat()

    def _on_close_chat(self):
        chat_data = self._get_selected_listbox_data(self._chats_list)
        if not chat_data:
            return
        cid = chat_data["id"]
        if cid in self._chat_states:
            self._close_chat(cid)
        self._refresh_chats()

    def _sync_input_for_active_chat(self):
        state = self._get_active_chat_state()
        if not state:
            self._disable_input()
            self._hide_thinking_indicator()
            self._cost_label.configure(text="Cost: $0.0000")
            return
        self._cost_label.configure(text=f"Cost: ${state.cost:.4f}")
        if self.theta_code and self.theta_code._running:
            self._docker_label.configure(text="Docker ready ✓")
        elif self._docker_starting:
            self._docker_label.configure(text="Building / starting Docker…")
        else:
            self._docker_label.configure(text="")
        if state.is_thinking:
            self._show_thinking_indicator()
            self._disable_input()
        elif state.chat is None:
            self._hide_thinking_indicator()
            self._disable_input()
        else:
            self._hide_thinking_indicator()
            self._enable_input()

    def _ensure_docker_running(self):
        pid = self.project_id
        if pid is None:
            return
        if self.theta_code is not None and self.theta_code._running:
            return
        if self._docker_starting:
            return
        proj_row = self.storage.get_project(pid)
        if not proj_row:
            return
        project = Project.from_path(proj_row["name"], proj_row["path"], proj_row.get("original_path"))
        def _docker_worker():
            try:
                if self.project_id != pid:
                    return
                tc = self.theta_code
                if tc is None:
                    tc = ThetaCode()
                    tc.set_project(project)
                    self.theta_code = tc
                if not tc._running:
                    self.root.after(0, lambda: self._docker_label.configure(text="Building / starting Docker…"))
                    tc.start_docker(recreate_venvs=False)
                    self.root.after(0, lambda: self._docker_label.configure(text="Docker ready ✓"))
            except Exception as exc:
                self.root.after(0, lambda e=str(exc): self._docker_label.configure(text=f"Docker error: {e}"))
            finally:
                self._docker_starting = False
        self._docker_starting = True
        threading.Thread(target=_docker_worker, daemon=True).start()

    # -----------------------------------------------------------------------
    # Merge dialog
    # -----------------------------------------------------------------------
    def _open_merge_dialog(self):
        if self.project_id is None:
            return
        proj = self.storage.get_project(self.project_id)
        if not proj:
            return
        working = Path(proj["path"])
        original = Path(proj.get("original_path") or proj["path"])
        if not working.exists() or not original.exists():
            messagebox.showerror("Merge error", "Working or original path missing.", parent=self.root)
            return
        changes = detect_changes(working, original)
        if not changes:
            messagebox.showinfo("Merge", "No changes to merge.", parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Merge Project")
        dialog.configure(bg=BG_DARK)
        dialog.geometry("900x650")
        dialog.minsize(600, 400)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.geometry("+%d+%d" % (self.root.winfo_rootx() + self.root.winfo_width() // 2 - 450, self.root.winfo_rooty() + self.root.winfo_height() // 2 - 325))
        toolbar = tk.Frame(dialog, bg=BG_SURFACE, padx=12, pady=8)
        toolbar.pack(fill=tk.X)
        def _gitignore_filter():
            matcher = GitignoreMatcher(original)
            for ch in changes:
                if matcher.is_ignored(ch.relative, is_dir=(ch.working_path and ch.working_path.is_dir())):
                    return True
            return False
        has_gitignored = _gitignore_filter()
        def select_all():
            for cb in checkboxes:
                cb[0].set(True)
        def select_none():
            for cb in checkboxes:
                cb[0].set(False)
        def skip_gitignored():
            matcher = GitignoreMatcher(original)
            for cb in checkboxes:
                change = cb[1]
                is_dir = bool(change.working_path and change.working_path.is_dir())
                if matcher.is_ignored(change.relative, is_dir=is_dir):
                    cb[0].set(False)
        tk.Button(toolbar, text="Select All", bg=BG_SURFACE_CONTAINER, fg=FG_PRIMARY, relief=tk.FLAT, bd=0, font=("TkDefaultFont", 10), activebackground="#3d3d3d", command=select_all).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(toolbar, text="Select None", bg=BG_SURFACE_CONTAINER, fg=FG_PRIMARY, relief=tk.FLAT, bd=0, font=("TkDefaultFont", 10), activebackground="#3d3d3d", command=select_none).pack(side=tk.LEFT, padx=(0, 8))
        if has_gitignored:
            tk.Button(toolbar, text="Skip Gitignored", bg=BG_SURFACE_CONTAINER, fg=FG_PRIMARY, relief=tk.FLAT, bd=0, font=("TkDefaultFont", 10), activebackground="#3d3d3d", command=skip_gitignored).pack(side=tk.LEFT, padx=(0, 8))
        paned = tk.PanedWindow(dialog, orient=tk.HORIZONTAL, bg="#3d3d3d", sashwidth=1, sashrelief=tk.FLAT)
        paned.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))
        left_frame = tk.Frame(paned, bg=BG_SURFACE)
        paned.add(left_frame, minsize=250, width=400)
        canvas = tk.Canvas(left_frame, bg=BG_SURFACE, highlightthickness=0)
        vsb = tk.Scrollbar(left_frame, orient=tk.VERTICAL, command=canvas.yview, bg=BG_SURFACE_CONTAINER)
        scroll_frame = tk.Frame(canvas, bg=BG_SURFACE)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        canvas_window = canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        def _on_canvas_configure(event):
            canvas.itemconfig(canvas_window, width=event.width)
        canvas.bind("<Configure>", _on_canvas_configure)
        checkboxes: list[tuple[tk.BooleanVar, t.Any]] = []
        type_colors = {"new": "#5c8a5c", "modified": "#8a8a5c", "deleted": "#8a5c5c"}
        for change in changes:
            row = tk.Frame(scroll_frame, bg=BG_SURFACE, padx=4, pady=2)
            row.pack(fill=tk.X)
            var = tk.BooleanVar(value=True)
            cb = tk.Checkbutton(row, variable=var, bg=BG_SURFACE, activebackground=BG_SURFACE, selectcolor=ACCENT_BLUE)
            cb.pack(side=tk.LEFT)
            tk.Label(row, text=f"[{change.change_type.upper()}]  {change.relative}", bg=BG_SURFACE, fg=type_colors.get(change.change_type, FG_PRIMARY), font=("TkDefaultFont", 10), anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)
            checkboxes.append((var, change))
        def _on_frame_configure(event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
        scroll_frame.bind("<Configure>", _on_frame_configure)
        right_frame = tk.Frame(paned, bg=BG_SURFACE)
        paned.add(right_frame, minsize=250)
        tk.Label(right_frame, text="Diff Preview", bg=BG_SURFACE, fg=FG_VARIANT, font=("TkDefaultFont", 10, "bold"), anchor="w").pack(fill=tk.X, padx=8, pady=(8, 4))
        diff_text = tk.Text(right_frame, bg=BG_SURFACE_LOW, fg=FG_PRIMARY, wrap=tk.WORD, state=tk.DISABLED, relief=tk.FLAT, bd=0, font=("TkDefaultFont", 10), padx=8, pady=8)
        diff_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        diff_text.tag_configure("diff_add", foreground="#7cff7c")
        diff_text.tag_configure("diff_rem", foreground="#ff7c7c")
        diff_text.tag_configure("diff_hunk", foreground=FG_VARIANT)
        diff_text.tag_configure("diff_file", foreground=ACCENT_BLUE)
        def _show_diff_for(change):
            diff_text.configure(state=tk.NORMAL)
            diff_text.delete("1.0", tk.END)
            if change.change_type == "modified":
                diff = make_diff(change)
                for line in diff.splitlines():
                    if line.startswith("+") and not line.startswith("+++"):
                        diff_text.insert(tk.END, line + "\n", "diff_add")
                    elif line.startswith("-") and not line.startswith("---"):
                        diff_text.insert(tk.END, line + "\n", "diff_rem")
                    elif line.startswith("@@"):
                        diff_text.insert(tk.END, line + "\n", "diff_hunk")
                    elif line.startswith("---") or line.startswith("+++"):
                        diff_text.insert(tk.END, line + "\n", "diff_file")
                    else:
                        diff_text.insert(tk.END, line + "\n")
            elif change.change_type == "new":
                diff_text.insert(tk.END, "New file:\n", "diff_file")
                if change.working_path and change.working_path.is_file():
                    try:
                        content = change.working_path.read_text()
                        lines = content.splitlines()
                        for line in lines[:100]:
                            diff_text.insert(tk.END, "+" + line + "\n", "diff_add")
                        if len(lines) > 100:
                            diff_text.insert(tk.END, f"... ({len(lines) - 100} more lines)\n", "diff_hunk")
                    except Exception:
                        diff_text.insert(tk.END, "[Binary or unreadable file]\n", "diff_hunk")
            elif change.change_type == "deleted":
                diff_text.insert(tk.END, "File deleted\n", "diff_rem")
                if change.original_path and change.original_path.is_file():
                    try:
                        content = change.original_path.read_text()
                        lines = content.splitlines()
                        for line in lines[:100]:
                            diff_text.insert(tk.END, "-" + line + "\n", "diff_rem")
                        if len(lines) > 100:
                            diff_text.insert(tk.END, f"... ({len(lines) - 100} more lines)\n", "diff_hunk")
                    except Exception:
                        diff_text.insert(tk.END, "[Binary or unreadable file]\n", "diff_hunk")
            diff_text.configure(state=tk.DISABLED)
        for var, change in checkboxes:
            for widget in scroll_frame.winfo_children():
                if isinstance(widget, tk.Frame):
                    for child in widget.winfo_children():
                        if isinstance(child, tk.Label) and change.relative in child.cget("text"):
                            child.bind("<Button-1>", lambda e, c=change: _show_diff_for(c))
        if checkboxes:
            _show_diff_for(checkboxes[0][1])
        btn_frame = tk.Frame(dialog, bg=BG_DARK, padx=12, pady=8)
        btn_frame.pack(fill=tk.X)
        def do_merge():
            selected = {cb[1].relative for cb in checkboxes if cb[0].get()}
            if not selected:
                dialog.destroy()
                return
            try:
                apply_changes(changes, selected)
            except Exception as exc:
                messagebox.showerror("Merge error", str(exc), parent=dialog)
                return
            dialog.destroy()
            messagebox.showinfo("Merge", f"Merged {len(selected)} change(s) successfully.", parent=self.root)
        tk.Button(btn_frame, text="Cancel", bg=BG_SURFACE_CONTAINER, fg=FG_PRIMARY, relief=tk.FLAT, bd=0, font=("TkDefaultFont", 10), activebackground="#3d3d3d", command=dialog.destroy).pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(btn_frame, text="Merge Selected", bg=ACCENT_BLUE, fg="#ffffff", relief=tk.FLAT, bd=0, font=("TkDefaultFont", 10, "bold"), activebackground="#5ab8ff", command=do_merge).pack(side=tk.RIGHT)

    def _confirm_delete_chat(self):
        chat_data = self._get_selected_listbox_data(self._chats_list)
        if not chat_data:
            return
        name = chat_data["name"]
        cid = chat_data["id"]
        if messagebox.askyesno("Delete chat?", f"Delete '{name}'?\n\nAll messages in this chat will be permanently deleted.", parent=self.root, icon="warning"):
            if cid in self._chat_states:
                self._close_chat(cid)
            self.storage.delete_chat(cid)
            self._refresh_chats()
            self._delete_chat_btn.configure(state=tk.DISABLED)
            self._copy_json_btn.configure(state=tk.DISABLED)
            self._close_chat_btn.configure(state=tk.DISABLED)

    def _open_new_chat_dialog(self):
        if self.project_id is None:
            messagebox.showinfo("No project selected", "Please select or create a project first.", parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("New Chat")
        dialog.configure(bg=BG_SURFACE_CONTAINER)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.geometry("+%d+%d" % (self.root.winfo_rootx() + self.root.winfo_width() // 2 - 150, self.root.winfo_rooty() + self.root.winfo_height() // 2 - 60))
        frame = tk.Frame(dialog, bg=BG_SURFACE_CONTAINER, padx=20, pady=20)
        frame.pack()
        tk.Label(frame, text="Chat Name", bg=BG_SURFACE_CONTAINER, fg=FG_PRIMARY, font=("TkDefaultFont", 10, "bold"), anchor="w").pack(fill=tk.X)
        name_var = tk.StringVar()
        name_entry = tk.Entry(frame, textvariable=name_var, bg=BG_SURFACE, fg=FG_PRIMARY, relief=tk.FLAT, bd=0, insertbackground=FG_PRIMARY, font=("TkDefaultFont", 11), width=30)
        name_entry.pack(fill=tk.X, pady=(4, 16))
        name_entry.focus_set()
        btn_frame = tk.Frame(frame, bg=BG_SURFACE_CONTAINER)
        btn_frame.pack(fill=tk.X)
        def create():
            name = name_var.get().strip()
            if not name:
                return
            cid = self.storage.create_chat(self.project_id, name)
            dialog.destroy()
            self._refresh_chats()
            self._select_chat_by_id(cid)
        tk.Button(btn_frame, text="Cancel", bg=BG_SURFACE, fg=FG_PRIMARY, relief=tk.FLAT, bd=0, font=("TkDefaultFont", 10), activebackground="#3d3d3d", activeforeground=FG_PRIMARY, command=dialog.destroy, cursor="hand2").pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(btn_frame, text="Create", bg=ACCENT_BLUE, fg="#ffffff", relief=tk.FLAT, bd=0, font=("TkDefaultFont", 10, "bold"), activebackground="#5ab8ff", activeforeground="#ffffff", command=create, cursor="hand2").pack(side=tk.RIGHT)
        dialog.bind("<Return>", lambda e: create())

    def _select_chat_by_id(self, cid: int):
        items = self.storage.get_chats(self.project_id)
        for i, item in enumerate(items):
            if item["id"] == cid:
                self._chats_list.select_clear(0, tk.END)
                self._chats_list.select_set(i)
                self._chats_list.see(i)
                self._on_chat_select()
                break

    # -----------------------------------------------------------------------
    # Input / send / cancel
    # -----------------------------------------------------------------------
    def _on_input_return(self, event=None):
        if event.state & 0x1:
            return
        self._do_send()
        return "break"

    def _on_input_shift_return(self, event=None):
        self._input_text.insert(tk.INSERT, "\n")
        return "break"

    def _enable_input(self):
        self._input_text.configure(state=tk.NORMAL)
        self._send_btn.configure(state=tk.NORMAL)
        self._cancel_btn.pack_forget()
        self._cancel_btn.configure(state=tk.DISABLED)
        self._model_entry.configure(state=tk.NORMAL)

    def _disable_input(self):
        self._input_text.configure(state=tk.DISABLED)
        self._send_btn.configure(state=tk.DISABLED)

    def _show_thinking_indicator(self):
        self._thinking_label.configure(text="AI is thinking…")
        self._thinking_progress.pack(side=tk.LEFT, padx=(8, 0))
        self._thinking_progress.start(10)
        self._cancel_btn.configure(state=tk.NORMAL)
        self._cancel_btn.pack(side=tk.RIGHT, padx=(8, 0))

    def _hide_thinking_indicator(self):
        self._thinking_label.configure(text="")
        self._thinking_progress.stop()
        self._thinking_progress.pack_forget()
        self._cancel_btn.pack_forget()
        self._cancel_btn.configure(state=tk.DISABLED)

    def _do_cancel(self):
        state = self._get_active_chat_state()
        if not state or not state.is_thinking:
            return
        evt = state.cancel_event
        if evt:
            evt.set()
        self._cancel_btn.configure(state=tk.DISABLED)
        self._thinking_label.configure(text="Cancelling…")

    def _do_send(self):
        text = self._input_text.get("1.0", tk.END).strip()
        state = self._get_active_chat_state()
        if not text or not state or state.is_thinking or not state.chat:
            return
        self._input_text.delete("1.0", tk.END)
        self._disable_input()
        state.is_thinking = True
        state.cancel_event = threading.Event()
        state.current_stream_placeholder = None
        self._show_thinking_indicator()

        chat_obj = state.chat
        llm_str = (self._model_var.get() or DEFAULT_MODEL).strip()
        try:
            llm = get_llm(llm_str)
        except ValueError as exc:
            self._add_message_bubble({
                "role": "assistant", "content": f"[Configuration error] {exc}\n\nPlease set a valid model name above.",
                "thinking": "", "cost": 0.0,
            }, chat_state=state)
            state.is_thinking = False
            state.cancel_event = None
            self._hide_thinking_indicator()
            self._enable_input()
            return

        user_msg = {"role": "user", "content": f"<user_message>\n{text}\n</user_message>"}
        self._persist_and_show(user_msg)
        ph = self._insert_streaming_placeholder(chat_state=state)
        state.current_stream_placeholder = ph
        assistant_final = [None]
        def on_token(content: str):
            state.stream_queue.put(("token", content))
        def on_new_msg(msg: dict):
            role = msg.get("role", "")
            if role == "assistant":
                content = msg.get("content", "")
                if "<tool_call>" in content and "</tool_call>" in content:
                    state.stream_queue.put(("assistant_tool", msg))
                else:
                    assistant_final[0] = msg
                return
            if msg.get("content", "").lstrip().startswith("<user_message>"):
                return
            state.stream_queue.put(("message", msg))
        cancel_event = state.cancel_event
        def _stream_worker():
            try:
                result = chat_obj.send_message_stream(
                    text, llm, on_token=on_token, on_new_message=on_new_msg, cancel_event=cancel_event,
                )
            except Exception as exc:
                state.stream_queue.put(("error", str(exc)))
            else:
                if cancel_event and cancel_event.is_set():
                    state.stream_queue.put(("cancelled", assistant_final[0]))
                else:
                    state.stream_queue.put(("done", assistant_final[0]))
        threading.Thread(target=_stream_worker, daemon=True).start()
        if not state._poll_running:
            self._poll_stream_queue(state.chat_id)

    # -----------------------------------------------------------------------
    # Stream queue polling
    # -----------------------------------------------------------------------
    def _poll_stream_queue(self, chat_id: int):
        state = self._chat_states.get(chat_id)
        if not state or state._poll_running:
            return
        state._poll_running = True
        try:
            while True:
                try:
                    item = state.stream_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    action = item[0]
                    if action == "token":
                        _, token = item
                        ph = state.current_stream_placeholder
                        if ph is None:
                            ph = self._insert_streaming_placeholder(chat_state=state)
                            state.current_stream_placeholder = ph
                        self._append_streaming_token(ph, token, chat_state=state)
                    elif action == "message":
                        _, msg = item
                        self._persist_and_show(msg, chat_state=state)
                    elif action == "assistant_tool":
                        _, msg = item
                        self.storage.append_message(
                            chat_id=chat_id, role="assistant",
                            content=msg.get("content", ""), thinking=msg.get("thinking", "") or "",
                            cost=msg.get("cost", 0.0) or 0.0, llm_model=msg.get("llm", "") or "",
                        )
                        ph = state.current_stream_placeholder
                        if ph:
                            self._finalize_streaming_bubble(ph, msg, chat_state=state)
                            self._update_chat_cost(state)
                        state.current_stream_placeholder = None
                    elif action == "error":
                        _, error = item
                        ph = state.current_stream_placeholder
                        if ph:
                            self._finalize_streaming_bubble(ph, None, error_msg=error, chat_state=state)
                        state.is_thinking = False
                        state.cancel_event = None
                        state.current_stream_placeholder = None
                        if self.active_chat_id == chat_id:
                            self._hide_thinking_indicator()
                            self._enable_input()
                            self._update_chat_cost(state)
                    elif action == "cancelled":
                        _, final_msg = item
                        ph = state.current_stream_placeholder
                        if ph is None and final_msg is not None:
                            ph = self._insert_streaming_placeholder(chat_state=state)
                            state.current_stream_placeholder = ph
                        if ph:
                            if final_msg is not None:
                                self._finalize_streaming_bubble(ph, final_msg, chat_state=state)
                                self.storage.append_message(
                                    chat_id=chat_id, role="assistant",
                                    content=final_msg.get("content", ""), thinking=final_msg.get("thinking", "") or "",
                                    cost=final_msg.get("cost", 0.0) or 0.0, llm_model=final_msg.get("llm", "") or "",
                                )
                            else:
                                self._finalize_streaming_bubble(ph, None, chat_state=state)
                        state.is_thinking = False
                        state.cancel_event = None
                        state.current_stream_placeholder = None
                        if self.active_chat_id == chat_id:
                            self._hide_thinking_indicator()
                            self._enable_input()
                            self._update_chat_cost(state)
                    elif action == "done":
                        _, final_msg = item
                        ph = state.current_stream_placeholder
                        if ph is None and final_msg is not None:
                            ph = self._insert_streaming_placeholder(chat_state=state)
                            state.current_stream_placeholder = ph
                        if ph:
                            if final_msg is not None:
                                self._finalize_streaming_bubble(ph, final_msg, chat_state=state)
                                self.storage.append_message(
                                    chat_id=chat_id, role="assistant",
                                    content=final_msg.get("content", ""), thinking=final_msg.get("thinking", "") or "",
                                    cost=final_msg.get("cost", 0.0) or 0.0, llm_model=final_msg.get("llm", "") or "",
                                )
                            else:
                                self._finalize_streaming_bubble(ph, None, chat_state=state)
                        state.is_thinking = False
                        state.cancel_event = None
                        state.current_stream_placeholder = None
                        if self.active_chat_id == chat_id:
                            self._hide_thinking_indicator()
                            self._enable_input()
                            self._update_chat_cost(state)
                except Exception:
                    import traceback
                    traceback.print_exc()
        finally:
            state._poll_running = False
        if state.is_thinking:
            self.root.after(50, lambda _cid=chat_id: self._poll_stream_queue(_cid))

    def _persist_and_show(self, msg: dict, chat_state: _ChatState | None = None):
        if chat_state is None:
            chat_state = self._get_active_chat_state()
            if not chat_state:
                return
        chat_id = chat_state.chat_id
        role = msg.get("role", "")
        if role == "system":
            return
        if chat_id:
            self.storage.append_message(
                chat_id=chat_id, role=role, content=msg.get("content", ""),
                thinking=msg.get("thinking", "") or "", cost=msg.get("cost", 0.0) or 0.0,
                llm_model=msg.get("llm", "") or "",
            )
        self._add_message_bubble(msg, chat_state=chat_state)
        self._update_chat_cost(chat_state)

    def _update_chat_cost(self, chat_state: _ChatState):
        chat_obj = chat_state.chat
        if chat_obj:
            chat_state.cost = chat_obj.get_cost()
        if self.active_chat_id == chat_state.chat_id:
            self._cost_label.configure(text=f"Cost: ${chat_state.cost:.4f}")

    # -----------------------------------------------------------------------
    # Copy chat as JSON
    # -----------------------------------------------------------------------
    def _copy_chat_as_json(self):
        chat_data = self._get_selected_listbox_data(self._chats_list)
        if not chat_data:
            return
        cid = chat_data["id"]
        messages = self.storage.get_messages(cid)
        json_str = json.dumps(messages, indent=2, ensure_ascii=False)
        self.root.clipboard_clear()
        self.root.clipboard_append(json_str)
        prev = self._docker_label.cget("text")
        self._docker_label.configure(text="Copied to clipboard ✓")
        self.root.after(2000, lambda: self._docker_label.configure(text=prev))

    # -----------------------------------------------------------------------
    def run(self):
        self.root.mainloop()


def main():
    app = ThetaCodeApp()
    app.run()


if __name__ == "__main__":
    main()
