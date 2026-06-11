"""
ThetaCode – tkinter desktop GUI
Run with:  python src/app.py
"""
import atexit
import os
import sys
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / '.env')

# Ensure the src package is importable when running as `python app.py`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from storage import Storage                        # noqa: E402
from main import Project, ThetaCode, Chat          # noqa: E402
from llm import get_llm                            # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_MODEL = "openrouter/nex-agi/nex-n2-pro:free"
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
        "user": "#1a3a5c",       # PRIMARY_CONTAINER
        "assistant": "#3a2a5c",  # SECONDARY_CONTAINER
    }.get(role, "#2d2d2d")       # SURFACE_CONTAINER


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
    """Strip XML wrapper tags that are internal scaffolding."""
    content = msg.get("content", "")
    for tag in ("user_message", "tool_response"):
        open_tag = f"<{tag}>"
        close_tag = f"</{tag}>"
        if content.lstrip().startswith(open_tag) and close_tag in content:
            content = content.split(open_tag, 1)[-1].split(close_tag, 1)[0].strip()
    return content


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

        # Dark theme via ttk style
        self._configure_style()

        self.storage = Storage()

        # ---- Mutable app state --------------------------------------------
        self.project_id = None
        self.chat_id = None
        self.theta_code = None   # ThetaCode | None
        self.chat = None         # Chat | None
        self.is_thinking = False

        # ---- Streaming queue ----------------------------------------------
        self._stream_queue: queue.Queue = queue.Queue()
        self._stream_token_counter = 0  # used to assign unique IDs to streaming bubbles

        # ---- Build UI -----------------------------------------------------
        self._build_ui()

        # ---- Cleanup on exit ----------------------------------------------
        atexit.register(self._cleanup)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Initial load
        self._refresh_projects()

    # -----------------------------------------------------------------------
    # Dark theme configuration
    # -----------------------------------------------------------------------
    def _configure_style(self):
        style = ttk.Style(self.root)
        # Use clam theme as base
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG_DARK, foreground=FG_PRIMARY)
        style.configure("TFrame", background=BG_DARK)
        style.configure("TLabel", background=BG_DARK, foreground=FG_PRIMARY)
        style.configure("TButton", background=BG_SURFACE_CONTAINER, foreground=FG_PRIMARY,
                        borderwidth=1, relief="flat", padding=(8, 4))
        style.map("TButton",
                  background=[("active", "#3d3d3d"), ("pressed", "#4d4d4d")])
        style.configure("TEntry", fieldbackground=BG_SURFACE_CONTAINER, foreground=FG_PRIMARY,
                        borderwidth=1, relief="solid")
        style.configure("TProgressbar", troughcolor=BG_SURFACE_CONTAINER, background=ACCENT_BLUE)
        style.configure("TNotebook", background=BG_DARK, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG_SURFACE_CONTAINER, foreground=FG_PRIMARY,
                        padding=(12, 4))
        style.configure("Vertical.TScrollbar", background=BG_SURFACE_CONTAINER,
                        troughcolor=BG_DARK, arrowcolor=FG_PRIMARY)
        style.configure("TSeparator", background="#3d3d3d")

        # Listbox styling (not ttk, so use option_add)
        self.root.option_add("*Listbox.background", BG_SURFACE_LOW)
        self.root.option_add("*Listbox.foreground", FG_PRIMARY)
        self.root.option_add("*Listbox.selectBackground", ACCENT_BLUE)
        self.root.option_add("*Listbox.selectForeground", "#ffffff")
        self.root.option_add("*Listbox.borderWidth", 0)
        self.root.option_add("*Listbox.highlightThickness", 0)
        self.root.option_add("*Listbox.font", ("TkDefaultFont", 11))

    # -----------------------------------------------------------------------
    # Build the full UI
    # -----------------------------------------------------------------------
    def _build_ui(self):
        # Main horizontal PanedWindow for the 3-panel layout
        self._paned = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, bg="#3d3d3d",
                                     sashwidth=1, sashrelief=tk.FLAT)
        self._paned.pack(fill=tk.BOTH, expand=True)

        # Left panel: Projects
        self._left_frame = tk.Frame(self._paned, bg=BG_SURFACE_LOW, width=SIDE_PANEL_WIDTH)
        self._paned.add(self._left_frame, minsize=150, width=SIDE_PANEL_WIDTH, stretch="never")
        self._build_projects_panel()

        # Middle panel: Chats
        self._mid_frame = tk.Frame(self._paned, bg=BG_SURFACE_LOW, width=SIDE_PANEL_WIDTH)
        self._paned.add(self._mid_frame, minsize=150, width=SIDE_PANEL_WIDTH, stretch="never")
        self._build_chats_panel()

        # Right panel: Messages + input
        self._right_frame = tk.Frame(self._paned, bg=BG_SURFACE)
        self._paned.add(self._right_frame, minsize=300, stretch="always")
        self._build_chat_panel()

    # -----------------------------------------------------------------------
    # Projects panel (left)
    # -----------------------------------------------------------------------
    def _build_projects_panel(self):
        f = self._left_frame

        # Header
        header = tk.Frame(f, bg=BG_SURFACE_LOW)
        header.pack(fill=tk.X, padx=12, pady=(10, 4))
        tk.Label(header, text="Projects", bg=BG_SURFACE_LOW, fg=FG_PRIMARY,
                 font=("TkDefaultFont", 12, "bold"), anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(header, text="+", bg=BG_SURFACE_CONTAINER, fg=FG_PRIMARY,
                  relief=tk.FLAT, bd=0, font=("TkDefaultFont", 14, "bold"),
                  activebackground="#3d3d3d", activeforeground=FG_PRIMARY,
                  command=self._open_new_project_dialog, cursor="hand2").pack(side=tk.RIGHT)

        tk.Frame(f, bg="#3d3d3d", height=1).pack(fill=tk.X, padx=4)

        # Project listbox with scrollbar
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

        # Delete button
        self._delete_project_btn = tk.Button(f, text="Delete Project", bg=BG_SURFACE_CONTAINER,
                                             fg=DANGER_RED, relief=tk.FLAT, bd=0,
                                             activebackground="#4d3d3d", activeforeground=DANGER_RED,
                                             font=("TkDefaultFont", 10), cursor="hand2",
                                             command=self._confirm_delete_project,
                                             state=tk.DISABLED)
        self._delete_project_btn.pack(fill=tk.X, padx=12, pady=(2, 10))

    # -----------------------------------------------------------------------
    # Chats panel (middle)
    # -----------------------------------------------------------------------
    def _build_chats_panel(self):
        f = self._mid_frame

        # Header
        header = tk.Frame(f, bg=BG_SURFACE_LOW)
        header.pack(fill=tk.X, padx=12, pady=(10, 4))
        tk.Label(header, text="Chats", bg=BG_SURFACE_LOW, fg=FG_PRIMARY,
                 font=("TkDefaultFont", 12, "bold"), anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(header, text="+", bg=BG_SURFACE_CONTAINER, fg=FG_PRIMARY,
                  relief=tk.FLAT, bd=0, font=("TkDefaultFont", 14, "bold"),
                  activebackground="#3d3d3d", activeforeground=FG_PRIMARY,
                  command=self._open_new_chat_dialog, cursor="hand2").pack(side=tk.RIGHT)

        tk.Frame(f, bg="#3d3d3d", height=1).pack(fill=tk.X, padx=4)

        # Chat listbox with scrollbar
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

        # Delete button
        self._delete_chat_btn = tk.Button(f, text="Delete Chat", bg=BG_SURFACE_CONTAINER,
                                          fg=DANGER_RED, relief=tk.FLAT, bd=0,
                                          activebackground="#4d3d3d", activeforeground=DANGER_RED,
                                          font=("TkDefaultFont", 10), cursor="hand2",
                                          command=self._confirm_delete_chat,
                                          state=tk.DISABLED)
        self._delete_chat_btn.pack(fill=tk.X, padx=12, pady=(2, 10))

    # -----------------------------------------------------------------------
    # Chat / messages panel (right)
    # -----------------------------------------------------------------------
    def _build_chat_panel(self):
        f = self._right_frame

        # Messages area: use a Text widget for styled bubbles
        msg_frame = tk.Frame(f, bg=BG_SURFACE)
        msg_frame.pack(fill=tk.BOTH, expand=True)

        self._messages_text = tk.Text(
            msg_frame, bg=BG_SURFACE, fg=FG_PRIMARY,
            wrap=tk.WORD, state=tk.DISABLED,
            relief=tk.FLAT, bd=0, highlightthickness=0,
            padx=12, pady=12, spacing1=4, spacing2=4, spacing3=0,
            font=("TkDefaultFont", 12),
        )
        msg_scrollbar = tk.Scrollbar(msg_frame, orient=tk.VERTICAL, bg=BG_SURFACE_CONTAINER,
                                     troughcolor=BG_DARK, activebackground="#4d4d4d")
        self._messages_text.configure(yscrollcommand=msg_scrollbar.set)
        msg_scrollbar.configure(command=self._messages_text.yview)
        self._messages_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        msg_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Configure tags for message bubbles
        self._messages_text.tag_configure("bubble_user", background=BG_PRIMARY_CONTAINER,
                                          lmargin1=10, lmargin2=10, rmargin=10,
                                          spacing1=4, spacing3=4, wrap=tk.WORD)
        self._messages_text.tag_configure("bubble_ai", background=BG_SECONDARY_CONTAINER,
                                          lmargin1=10, lmargin2=10, rmargin=10,
                                          spacing1=4, spacing3=4, wrap=tk.WORD)
        self._messages_text.tag_configure("bubble_tool", background=BG_SURFACE_CONTAINER,
                                          lmargin1=10, lmargin2=10, rmargin=10,
                                          spacing1=4, spacing3=4, wrap=tk.WORD)
        self._messages_text.tag_configure("bubble_error", background="#5c2a2a",
                                          lmargin1=10, lmargin2=10, rmargin=10,
                                          spacing1=4, spacing3=4, wrap=tk.WORD)
        self._messages_text.tag_configure("role_label", foreground=FG_VARIANT,
                                          font=("TkDefaultFont", 10, "bold"))
        self._messages_text.tag_configure("cost_label", foreground=FG_VARIANT,
                                          font=("TkDefaultFont", 9))
        self._messages_text.tag_configure("thinking_header", foreground=FG_TERTIARY,
                                          font=("TkDefaultFont", 10, "italic"))
        self._messages_text.tag_configure("thinking_body", foreground=FG_VARIANT,
                                          font=("TkDefaultFont", 10))
        self._messages_text.tag_configure("content", foreground=FG_PRIMARY,
                                          font=("TkDefaultFont", 12))
        self._messages_text.tag_configure("error_text", foreground="#ff6b6b",
                                          font=("TkDefaultFont", 12))

        # Override the built-in "sel" tag so selection highlighting renders
        # above custom bubble backgrounds
        self._messages_text.tag_configure("sel", background="#4a9eff",
                                          foreground="#ffffff")
        self._messages_text.tag_raise("sel")

        # Thinking expand/collapse tracking
        self._thinking_blocks: dict[str, dict] = {}  # block_id -> {start, end, hidden_start, hidden_end, visible}

        # Bottom bar
        bottom = tk.Frame(f, bg=BG_SURFACE)
        bottom.pack(fill=tk.X, side=tk.BOTTOM)

        tk.Frame(bottom, bg="#3d3d3d", height=1).pack(fill=tk.X)

        # Status + model row
        status_row = tk.Frame(bottom, bg=BG_SURFACE)
        status_row.pack(fill=tk.X, padx=12, pady=(6, 4))

        self._thinking_label = tk.Label(status_row, text="", bg=BG_SURFACE, fg=FG_VARIANT,
                                        font=("TkDefaultFont", 11, "italic"))
        self._thinking_label.pack(side=tk.LEFT)

        # Thinking progress bar
        self._thinking_progress = ttk.Progressbar(status_row, mode="indeterminate",
                                                  length=20, style="TProgressbar")
        # Hidden initially

        # Right side of status row
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

        # Input row
        input_row = tk.Frame(bottom, bg=BG_SURFACE)
        input_row.pack(fill=tk.X, padx=12, pady=(4, 12))

        self._input_text = tk.Text(input_row, bg=BG_SURFACE_CONTAINER, fg=FG_PRIMARY,
                                   relief=tk.FLAT, bd=0, insertbackground=FG_PRIMARY,
                                   font=("TkDefaultFont", 12), height=2, wrap=tk.WORD,
                                   padx=10, pady=8)
        self._input_text.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        # Bind Enter to send, Shift+Enter for newline
        self._input_text.bind("<Return>", self._on_input_return)
        self._input_text.bind("<Shift-Return>", self._on_input_shift_return)

        # Disable input initially
        self._input_text.configure(state=tk.DISABLED)

        self._send_btn = tk.Button(input_row, text="Send", bg=ACCENT_BLUE, fg="#ffffff",
                                   relief=tk.FLAT, bd=0, font=("TkDefaultFont", 11, "bold"),
                                   activebackground="#5ab8ff", activeforeground="#ffffff",
                                   command=self._do_send, cursor="hand2",
                                   state=tk.DISABLED, padx=16, pady=4)
        self._send_btn.pack(side=tk.RIGHT)

    # -----------------------------------------------------------------------
    # Cleanup
    # -----------------------------------------------------------------------
    def _cleanup(self):
        tc = self.theta_code
        if tc:
            try:
                tc.stop_docker()
            except Exception:
                pass

    def _on_close(self):
        """Show a shutdown indicator while stopping Docker, then destroy."""
        # Prevent double-close
        if hasattr(self, '_shutting_down') and self._shutting_down:
            return
        self._shutting_down = True

        # Hide all panels
        self._paned.pack_forget()

        # Create full-window overlay
        self._shutdown_overlay = tk.Frame(self.root, bg=BG_DARK)
        self._shutdown_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)

        tk.Label(
            self._shutdown_overlay, bg=BG_DARK, fg=FG_PRIMARY,
            text="Shutting down ThetaCode…",
            font=("TkDefaultFont", 18, "bold"),
        ).pack(expand=True, pady=(0, 12))

        progress = ttk.Progressbar(
            self._shutdown_overlay, mode="indeterminate",
            length=200, style="TProgressbar",
        )
        progress.pack(expand=True)
        progress.start(10)

        # Run cleanup in background thread
        cleanup_done = [False]

        def _do_cleanup():
            self._cleanup()
            cleanup_done[0] = True

        threading.Thread(target=_do_cleanup, daemon=True).start()

        # Poll until cleanup finishes, with a timeout
        start_time = [time.time()]

        def _poll_shutdown():
            elapsed = time.time() - start_time[0]
            if cleanup_done[0] or elapsed > 5.0:  # 5-second safety timeout
                progress.stop()
                self.root.destroy()
            else:
                self.root.after(100, _poll_shutdown)

        self.root.after(100, _poll_shutdown)

    # -----------------------------------------------------------------------
    # Message display helpers
    # -----------------------------------------------------------------------
    def _populate_listbox(self, listbox: tk.Listbox, items: list, data_key: str):
        """Populate a listbox with item names, storing IDs."""
        listbox.delete(0, tk.END)
        for i, item in enumerate(items):
            listbox.insert(tk.END, item["name"])
            listbox._cached_data = getattr(listbox, "_cached_data", {})
            listbox._cached_data[i] = item

    def _get_selected_listbox_data(self, listbox: tk.Listbox) -> dict | None:
        selection = listbox.curselection()
        if not selection:
            return None
        idx = selection[0]
        data = getattr(listbox, "_cached_data", {})
        return data.get(idx)

    def _add_message_bubble(self, msg: dict):
        """Append a styled bubble to the messages Text widget."""
        role = msg.get("role", "")
        if role == "system":
            return

        text_widget = self._messages_text
        text_widget.configure(state=tk.NORMAL)

        label = _role_label(msg)
        content = _display_content(msg)
        thinking = msg.get("thinking", "") or ""
        cost = msg.get("cost", 0.0) or 0.0
        is_error = "[Runtime error]" in content or "[Configuration error]" in content

        # Choose tag based on role
        if is_error:
            bubble_tag = "bubble_error"
        elif role == "user":
            if content.lstrip().startswith("<tool_response>"):
                content = _display_content(msg)
                bubble_tag = "bubble_tool"
            else:
                bubble_tag = "bubble_user"
        else:
            bubble_tag = "bubble_ai"

        # Start of bubble (store position for potential thinking collapse)
        bubble_start = text_widget.index(tk.END + "-1c")
        if bubble_start.startswith("1.0"):
            # If this is the first insertion, remove the auto line start
            pass

        # Role label
        text_widget.insert(tk.END, f"{label}\n", (bubble_tag, "role_label"))

        # Thinking block
        thinking_hidden_start = None
        thinking_hidden_end = None
        if thinking:
            thinking_id = f"thinking_{id(msg)}_{len(self._thinking_blocks)}"
            unique_header_tag = f"thinking_header_{thinking_id}"
            unique_body_tag = f"thinking_body_{thinking_id}"

            # Configure unique tags with the same visual style
            text_widget.tag_configure(unique_header_tag, foreground="#7eb6ff",
                                      font=("TkDefaultFont", 10, "italic"))
            text_widget.tag_configure(unique_body_tag, foreground="#a0a0a0",
                                      font=("TkDefaultFont", 10))

            text_widget.insert(tk.END, "🧠 Show / hide thinking\n", (bubble_tag, unique_header_tag))
            thinking_hidden_start = text_widget.index(tk.END + "-1c")

            text_widget.insert(tk.END, f"{thinking}\n", (bubble_tag, unique_body_tag))
            thinking_hidden_end = text_widget.index(tk.END + "-1c")

            # Save the thinking text so we can restore it after hide
            saved_thinking = text_widget.get(thinking_hidden_start, thinking_hidden_end)

            # Store for toggle
            self._thinking_blocks[thinking_id] = {
                "start": thinking_hidden_start,
                "end": thinking_hidden_end,
                "visible": True,
                "body_tag": unique_body_tag,
                "header_tag": unique_header_tag,
                "bubble_tag": bubble_tag,
                "saved_content": saved_thinking,
            }

            # Bind click handler to the UNIQUE header tag (not shared)
            text_widget.tag_bind(unique_header_tag, "<Button-1>",
                                 lambda e, bid=thinking_id: self._toggle_thinking(bid))

        # Content
        text_widget.insert(tk.END, f"{content}\n", (bubble_tag, "content"))

        # Cost
        if cost > 0:
            text_widget.insert(tk.END, f"${cost:.6f}\n", (bubble_tag, "cost_label"))

        # Separator gap
        text_widget.insert(tk.END, "\n")

        text_widget.configure(state=tk.DISABLED)
        self._scroll_to_bottom()

    def _insert_streaming_placeholder(self) -> str:
        """Insert an empty AI bubble that will be updated with tokens. Returns a
        placeholder ID for later replacement."""
        text_widget = self._messages_text
        text_widget.configure(state=tk.NORMAL)

        bubble_start = text_widget.index(tk.END + "-1c")

        text_widget.insert(tk.END, "AI\n", ("bubble_ai", "role_label"))
        text_widget.insert(tk.END, "\n", ("bubble_ai", "content"))  # placeholder line

        text_widget.configure(state=tk.DISABLED)
        self._scroll_to_bottom()

        self._stream_token_counter += 1
        placeholder_id = f"_stream_{self._stream_token_counter}"

        # Store the range for this streaming bubble
        content_start = text_widget.index(f"{bubble_start}+1l")
        text_widget._stream_placeholders = getattr(text_widget, "_stream_placeholders", {})
        text_widget._stream_placeholders[placeholder_id] = {
            "start": content_start,
            "bubble_start": bubble_start,
            "tokens": [],
            "finalized": False,
        }

        return placeholder_id

    def _append_streaming_token(self, placeholder_id: str, token: str):
        """Append a token to the streaming bubble."""
        text_widget = self._messages_text
        placeholders = getattr(text_widget, "_stream_placeholders", {})
        info = placeholders.get(placeholder_id)
        if not info or info.get("finalized"):
            return

        info["tokens"].append(token)

        text_widget.configure(state=tk.NORMAL)
        # Delete old content line and re-insert full text
        content_start = info["start"]
        content_end = f"{content_start} lineend"
        text_widget.delete(content_start, content_end)
        full_text = "".join(info["tokens"])
        text_widget.insert(content_start, f"{full_text}\n", ("bubble_ai", "content"))
        text_widget.configure(state=tk.DISABLED)
        self._scroll_to_bottom()

    def _finalize_streaming_bubble(self, placeholder_id: str, final_msg: dict | None,
                                   error_msg: str | None = None):
        """Replace the streaming placeholder with the final assistant bubble or error."""
        text_widget = self._messages_text
        placeholders = getattr(text_widget, "_stream_placeholders", {})
        info = placeholders.get(placeholder_id)
        if not info:
            return

        info["finalized"] = True

        text_widget.configure(state=tk.NORMAL)

        bubble_start = info["start"].split(".")[0] + ".0"  # start of the role line
        # Find end: scan forward for next content
        # Simpler: just delete from bubble_start to end, then re-add
        # Actually, better: we just replace the content line
        content_start = info["start"]
        content_end = f"{content_start} lineend+1l"

        # Delete the streaming line
        text_widget.delete(content_start, content_end)

        if error_msg:
            text_widget.insert(content_start, f"[Runtime error] {error_msg}\n",
                              ("bubble_ai", "error_text"))
            text_widget.insert(content_start + "+1l", "\n")
            self._cost_label.configure(text="Cost: $0.0000")
        elif final_msg:
            final_content = final_msg.get("content", "")
            thinking = final_msg.get("thinking", "") or ""
            cost_val = final_msg.get("cost", 0.0) or 0.0

            # Add thinking if present
            if thinking:
                thinking_id = f"thinking_final_{placeholder_id}"
                unique_header_tag = f"thinking_header_{thinking_id}"
                unique_body_tag = f"thinking_body_{thinking_id}"

                # Configure unique tags with the same visual style
                text_widget.tag_configure(unique_header_tag, foreground="#7eb6ff",
                                          font=("TkDefaultFont", 10, "italic"))
                text_widget.tag_configure(unique_body_tag, foreground="#a0a0a0",
                                          font=("TkDefaultFont", 10))

                text_widget.insert(content_start, "🧠 Show / hide thinking\n",
                                  ("bubble_ai", unique_header_tag))
                thinking_hidden_start = text_widget.index(content_start + "+1l")
                text_widget.insert(thinking_hidden_start, f"{thinking}\n",
                                   ("bubble_ai", unique_body_tag))
                thinking_hidden_end = text_widget.index(thinking_hidden_start + "+1l")

                # Save the thinking text so we can restore it after hide
                saved_thinking = text_widget.get(thinking_hidden_start, thinking_hidden_end)

                self._thinking_blocks[thinking_id] = {
                    "start": thinking_hidden_start,
                    "end": thinking_hidden_end,
                    "visible": True,
                    "body_tag": unique_body_tag,
                    "header_tag": unique_header_tag,
                    "bubble_tag": "bubble_ai",
                    "saved_content": saved_thinking,
                }
                text_widget.tag_bind(unique_header_tag, "<Button-1>",
                                     lambda e, bid=thinking_id: self._toggle_thinking(bid))

                # Insert final content after thinking
                next_pos = thinking_hidden_end
                text_widget.insert(next_pos, f"{final_content}\n", ("bubble_ai", "content"))
            else:
                text_widget.insert(content_start, f"{final_content}\n", ("bubble_ai", "content"))

            if cost_val > 0:
                end_pos = text_widget.index(tk.END + "-1c")
                text_widget.insert(end_pos, f"${cost_val:.6f}\n", ("bubble_ai", "cost_label"))

            # Update cost display
            chat_obj = self.chat
            if chat_obj:
                self._cost_label.configure(text=f"Cost: ${chat_obj.get_cost():.4f}")

            # Separator
            text_widget.insert(tk.END, "\n")

        text_widget.configure(state=tk.DISABLED)
        self._scroll_to_bottom()

        # Clean up placeholder
        if placeholder_id in placeholders:
            del placeholders[placeholder_id]

    def _scroll_to_bottom(self):
        self._messages_text.see(tk.END)

    def _toggle_thinking(self, thinking_id: str):
        """Toggle visibility of a thinking block."""
        info = self._thinking_blocks.get(thinking_id)
        if not info:
            return

        text_widget = self._messages_text
        text_widget.configure(state=tk.NORMAL)

        if info["visible"]:
            # Hide: save the current content, then delete the thinking lines
            # Re-read the text in case it changed (unlikely but safe)
            try:
                current_text = text_widget.get(info["start"], info["end"])
                if current_text.strip():
                    info["saved_content"] = current_text
            except tk.TclError:
                pass
            text_widget.delete(info["start"], info["end"])
            info["visible"] = False
        else:
            # Show: re-insert the saved content with the proper tags
            bubble_tag = info.get("bubble_tag", "bubble_ai")
            body_tag = info.get("body_tag", "thinking_body")
            saved = info.get("saved_content", "")
            if saved:
                text_widget.insert(info["start"], saved, (bubble_tag, body_tag))
                # Adjust end position to match the restored content
                new_end = text_widget.index(f"{info['start']}+{len(saved.split(chr(10)))-1}l lineend")
                info["end"] = new_end
            info["visible"] = True

        text_widget.configure(state=tk.DISABLED)

    # -----------------------------------------------------------------------
    # Project list operations
    # -----------------------------------------------------------------------
    def _refresh_projects(self):
        items = self.storage.get_projects()
        self._populate_listbox(self._projects_list, items, "id")

        # Re-select if previously selected
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

        # Tear down old docker
        old_tc = self.theta_code
        if old_tc:
            threading.Thread(target=old_tc.stop_docker, daemon=True).start()
            self.theta_code = None

        self.project_id = pid
        self.chat_id = None
        self.chat = None
        self._clear_messages()
        self._cost_label.configure(text="Cost: $0.0000")
        self._docker_label.configure(text="")
        self._disable_input()
        self._delete_project_btn.configure(state=tk.NORMAL)
        self._refresh_projects()
        self._refresh_chats()

    def _confirm_delete_project(self):
        if self.project_id is None:
            return
        proj_data = self._get_selected_listbox_data(self._projects_list)
        name = proj_data["name"] if proj_data else "this project"

        if messagebox.askyesno(
            "Delete project?",
            f"Delete '{name}'?\n\nAll chats and messages will be permanently deleted.",
            parent=self.root,
            icon="warning",
        ):
            if proj_data and self.project_id == proj_data["id"]:
                old_tc = self.theta_code
                if old_tc:
                    threading.Thread(target=old_tc.stop_docker, daemon=True).start()
                    self.theta_code = None
                self.project_id = None
                self.chat_id = None
                self.chat = None
                self._clear_messages()
                self._disable_input()
                self._docker_label.configure(text="")
                self._refresh_chats()

            if proj_data:
                self.storage.delete_project(proj_data["id"])
            self._refresh_projects()
            self._delete_project_btn.configure(state=tk.DISABLED)

    def _open_new_project_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("New Project")
        dialog.configure(bg=BG_SURFACE_CONTAINER)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        # Center on parent
        dialog.geometry("+%d+%d" % (
            self.root.winfo_rootx() + self.root.winfo_width() // 2 - 220,
            self.root.winfo_rooty() + self.root.winfo_height() // 2 - 80,
        ))

        frame = tk.Frame(dialog, bg=BG_SURFACE_CONTAINER, padx=20, pady=20)
        frame.pack()

        tk.Label(frame, text="Project Name", bg=BG_SURFACE_CONTAINER, fg=FG_PRIMARY,
                 font=("TkDefaultFont", 10, "bold"), anchor="w").pack(fill=tk.X)
        name_var = tk.StringVar()
        name_entry = tk.Entry(frame, textvariable=name_var, bg=BG_SURFACE, fg=FG_PRIMARY,
                              relief=tk.FLAT, bd=0, insertbackground=FG_PRIMARY,
                              font=("TkDefaultFont", 11), width=40)
        name_entry.pack(fill=tk.X, pady=(4, 12))
        name_entry.focus_set()

        tk.Label(frame, text="Project Path", bg=BG_SURFACE_CONTAINER, fg=FG_PRIMARY,
                 font=("TkDefaultFont", 10, "bold"), anchor="w").pack(fill=tk.X)
        path_frame = tk.Frame(frame, bg=BG_SURFACE_CONTAINER)
        path_frame.pack(fill=tk.X, pady=(4, 16))

        path_var = tk.StringVar()
        path_entry = tk.Entry(path_frame, textvariable=path_var, bg=BG_SURFACE, fg=FG_PRIMARY,
                              relief=tk.FLAT, bd=0, insertbackground=FG_PRIMARY,
                              font=("TkDefaultFont", 11), width=32)
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        def browse():
            path = filedialog.askdirectory(title="Select project folder", parent=dialog)
            if path:
                path_var.set(path)

        tk.Button(path_frame, text="📁", bg=BG_SURFACE_CONTAINER, fg=FG_PRIMARY,
                  relief=tk.FLAT, bd=0, font=("TkDefaultFont", 14),
                  activebackground="#3d3d3d", activeforeground=FG_PRIMARY,
                  command=browse, cursor="hand2").pack(side=tk.RIGHT, padx=(4, 0))

        error_label = tk.Label(frame, text="", bg=BG_SURFACE_CONTAINER, fg=DANGER_RED,
                               font=("TkDefaultFont", 10))

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
                pid = self.storage.create_project(name, path)
            except Exception as exc:
                error_label.configure(text=str(exc))
                error_label.pack(fill=tk.X, pady=(0, 8))
                return

            dialog.destroy()
            self._refresh_projects()
            # Simulate selection
            self._select_project_by_id(pid)

        tk.Button(btn_frame, text="Cancel", bg=BG_SURFACE, fg=FG_PRIMARY,
                  relief=tk.FLAT, bd=0, font=("TkDefaultFont", 10),
                  activebackground="#3d3d3d", activeforeground=FG_PRIMARY,
                  command=dialog.destroy, cursor="hand2").pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(btn_frame, text="Create", bg=ACCENT_BLUE, fg="#ffffff",
                  relief=tk.FLAT, bd=0, font=("TkDefaultFont", 10, "bold"),
                  activebackground="#5ab8ff", activeforeground="#ffffff",
                  command=create, cursor="hand2").pack(side=tk.RIGHT)

        # Bind Enter
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
        if self.project_id is None:
            return

        items = self.storage.get_chats(self.project_id)
        self._populate_listbox(self._chats_list, items, "id")

        # Re-select if previously selected
        if self.chat_id is not None:
            for i, item in enumerate(items):
                if item["id"] == self.chat_id:
                    self._chats_list.select_set(i)
                    self._chats_list.see(i)
                    break

    def _on_chat_select(self, event=None):
        data = self._get_selected_listbox_data(self._chats_list)
        if not data:
            return
        cid = data["id"]
        if self.chat_id == cid:
            return
        self._select_chat(cid)

    def _select_chat(self, chat_id: int):
        self.chat_id = chat_id
        self.chat = None
        self._disable_input()
        self._clear_messages()
        self._docker_label.configure(text="Starting Docker environment…")
        self._cost_label.configure(text="Cost: $0.0000")
        self._delete_chat_btn.configure(state=tk.NORMAL)
        self._refresh_chats()

        proj_row = self.storage.get_project(self.project_id)
        if not proj_row:
            return

        project = Project.from_path(proj_row["name"], proj_row["path"])
        stored_msgs = self.storage.get_messages(chat_id)

        def _init_docker():
            tc = self.theta_code
            if tc is None:
                try:
                    tc = ThetaCode()
                    tc.set_project(project)
                    self.theta_code = tc
                except Exception as exc:
                    self._docker_label.configure(text=f"Docker init error: {exc}")
                    return

            if not tc._running:
                try:
                    self._docker_label.configure(text="Building / starting Docker…")
                    tc.start_docker(recreate_venvs=False)
                except Exception as exc:
                    self._docker_label.configure(text=f"Docker start error: {exc}")
                    return

            # Only proceed if this chat is still active
            if self.chat_id != chat_id:
                return

            chat_obj = Chat(project, tc)
            chat_obj.restore_messages(stored_msgs)
            self.chat = chat_obj

            self._cost_label.configure(text=f"Cost: ${chat_obj.get_cost():.4f}")
            self._docker_label.configure(text="Docker ready ✓")

            # Show stored messages in UI via main thread
            def _show_messages():
                self._clear_messages()
                for msg in stored_msgs:
                    if msg["role"] == "system":
                        continue
                    self._add_message_bubble({
                        "role": msg["role"],
                        "content": msg["content"],
                        "thinking": msg.get("thinking", ""),
                        "cost": msg.get("cost", 0.0),
                        "llm": msg.get("llm_model", ""),
                    })
                self._enable_input()

            self.root.after(0, _show_messages)

        threading.Thread(target=_init_docker, daemon=True).start()

    def _confirm_delete_chat(self):
        if self.chat_id is None:
            return
        chat_data = self._get_selected_listbox_data(self._chats_list)
        name = chat_data["name"] if chat_data else "this chat"

        if messagebox.askyesno(
            "Delete chat?",
            f"Delete '{name}'?\n\nAll messages in this chat will be permanently deleted.",
            parent=self.root,
            icon="warning",
        ):
            cid = self.chat_id
            if self.chat_id == cid:
                self.chat_id = None
                self.chat = None
                self._clear_messages()
                self._disable_input()
                self._cost_label.configure(text="Cost: $0.0000")
                self._docker_label.configure(text="")
            self.storage.delete_chat(cid)
            self._refresh_chats()
            self._delete_chat_btn.configure(state=tk.DISABLED)

    def _open_new_chat_dialog(self):
        if self.project_id is None:
            messagebox.showinfo(
                "No project selected",
                "Please select or create a project first.",
                parent=self.root,
            )
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("New Chat")
        dialog.configure(bg=BG_SURFACE_CONTAINER)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        dialog.geometry("+%d+%d" % (
            self.root.winfo_rootx() + self.root.winfo_width() // 2 - 150,
            self.root.winfo_rooty() + self.root.winfo_height() // 2 - 60,
        ))

        frame = tk.Frame(dialog, bg=BG_SURFACE_CONTAINER, padx=20, pady=20)
        frame.pack()

        tk.Label(frame, text="Chat Name", bg=BG_SURFACE_CONTAINER, fg=FG_PRIMARY,
                 font=("TkDefaultFont", 10, "bold"), anchor="w").pack(fill=tk.X)
        name_var = tk.StringVar()
        name_entry = tk.Entry(frame, textvariable=name_var, bg=BG_SURFACE, fg=FG_PRIMARY,
                              relief=tk.FLAT, bd=0, insertbackground=FG_PRIMARY,
                              font=("TkDefaultFont", 11), width=30)
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

        tk.Button(btn_frame, text="Cancel", bg=BG_SURFACE, fg=FG_PRIMARY,
                  relief=tk.FLAT, bd=0, font=("TkDefaultFont", 10),
                  activebackground="#3d3d3d", activeforeground=FG_PRIMARY,
                  command=dialog.destroy, cursor="hand2").pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(btn_frame, text="Create", bg=ACCENT_BLUE, fg="#ffffff",
                  relief=tk.FLAT, bd=0, font=("TkDefaultFont", 10, "bold"),
                  activebackground="#5ab8ff", activeforeground="#ffffff",
                  command=create, cursor="hand2").pack(side=tk.RIGHT)

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
    # Send message
    # -----------------------------------------------------------------------
    def _on_input_return(self, event=None):
        """Enter key pressed: send message (unless Shift is held)."""
        # Check if Shift is also pressed (handled separately)
        if (event.state & 0x1):  # Shift mask
            return  # Let _on_input_shift_return handle it
        self._do_send()
        return "break"  # Prevent default newline

    def _on_input_shift_return(self, event=None):
        """Shift+Enter: insert newline."""
        self._input_text.insert(tk.INSERT, "\n")
        return "break"

    def _enable_input(self):
        self._input_text.configure(state=tk.NORMAL)
        self._send_btn.configure(state=tk.NORMAL)
        self._model_entry.configure(state=tk.NORMAL)

    def _disable_input(self):
        self._input_text.configure(state=tk.DISABLED)
        self._send_btn.configure(state=tk.DISABLED)

    def _clear_messages(self):
        self._messages_text.configure(state=tk.NORMAL)
        self._messages_text.delete("1.0", tk.END)
        self._messages_text.configure(state=tk.DISABLED)
        self._thinking_blocks.clear()

    def _do_send(self):
        text = self._input_text.get("1.0", tk.END).strip()
        if not text or self.is_thinking or not self.chat:
            return

        self._input_text.delete("1.0", tk.END)
        self._disable_input()
        self.is_thinking = True
        self._show_thinking_indicator()

        chat_obj = self.chat
        llm_str = (self._model_var.get() or DEFAULT_MODEL).strip()

        try:
            llm = get_llm(llm_str)
        except ValueError as exc:
            self._add_message_bubble({
                "role": "assistant",
                "content": f"[Configuration error] {exc}\n\nPlease set a valid model name above.",
                "thinking": "",
                "cost": 0.0,
            })
            self.is_thinking = False
            self._hide_thinking_indicator()
            self._enable_input()
            return

        # Insert streaming placeholder
        placeholder_id = self._insert_streaming_placeholder()

        assistant_final = [None]  # mutable container for closure

        def on_token(content: str):
            self._stream_queue.put(("token", placeholder_id, content))

        def on_new_msg(msg: dict):
            role = msg.get("role", "")
            if role == "assistant":
                assistant_final[0] = msg
                return
            self._stream_queue.put(("message", msg))

        def _stream_worker():
            nonlocal assistant_final
            try:
                result = chat_obj.send_message_stream(
                    text, llm,
                    on_token=on_token,
                    on_new_message=on_new_msg,
                )
            except Exception as exc:
                self._stream_queue.put(("error", placeholder_id, str(exc)))
            else:
                self._stream_queue.put(("done", placeholder_id, assistant_final[0]))

        threading.Thread(target=_stream_worker, daemon=True).start()

        # Start polling the queue
        self._poll_stream_queue()

    def _poll_stream_queue(self):
        """Poll the stream queue and update UI on the main thread."""
        try:
            while True:
                item = self._stream_queue.get_nowait()
                action = item[0]

                if action == "token":
                    _, placeholder_id, token = item
                    self._append_streaming_token(placeholder_id, token)

                elif action == "message":
                    _, msg = item
                    self._persist_and_show(msg)

                elif action == "error":
                    _, placeholder_id, error = item
                    self._finalize_streaming_bubble(placeholder_id, None, error_msg=error)
                    self.is_thinking = False
                    self._hide_thinking_indicator()
                    self._enable_input()

                elif action == "done":
                    _, placeholder_id, final_msg = item
                    if final_msg is not None:
                        self._finalize_streaming_bubble(placeholder_id, final_msg)
                        # Persist to storage
                        chat_id = self.chat_id
                        if chat_id:
                            final_content = final_msg.get("content", "")
                            thinking = final_msg.get("thinking", "") or ""
                            cost_val = final_msg.get("cost", 0.0) or 0.0
                            llm_name = final_msg.get("llm", "") or ""
                            self.storage.append_message(
                                chat_id=chat_id,
                                role="assistant",
                                content=final_content,
                                thinking=thinking,
                                cost=cost_val,
                                llm_model=llm_name,
                            )
                    else:
                        # No final message — remove streaming placeholder
                        pass

                    # Update cost
                    chat_obj = self.chat
                    if chat_obj:
                        self._cost_label.configure(text=f"Cost: ${chat_obj.get_cost():.4f}")

                    self.is_thinking = False
                    self._hide_thinking_indicator()
                    self._enable_input()

        except queue.Empty:
            pass

        # Continue polling if still thinking
        if self.is_thinking:
            self.root.after(50, self._poll_stream_queue)

    def _show_thinking_indicator(self):
        self._thinking_label.configure(text="AI is thinking…")
        self._thinking_progress.pack(side=tk.LEFT, padx=(8, 0))
        self._thinking_progress.start(10)

    def _hide_thinking_indicator(self):
        self._thinking_label.configure(text="")
        self._thinking_progress.stop()
        self._thinking_progress.pack_forget()

    def _persist_and_show(self, msg: dict):
        """Persist a message to storage and add it to the UI."""
        chat_id = self.chat_id
        role = msg.get("role", "")
        if role == "system":
            return
        if chat_id:
            self.storage.append_message(
                chat_id=chat_id,
                role=role,
                content=msg.get("content", ""),
                thinking=msg.get("thinking", "") or "",
                cost=msg.get("cost", 0.0) or 0.0,
                llm_model=msg.get("llm", "") or "",
            )
        self._add_message_bubble(msg)
        # Update cost
        chat_obj = self.chat
        if chat_obj:
            self._cost_label.configure(text=f"Cost: ${chat_obj.get_cost():.4f}")

    # -----------------------------------------------------------------------
    # Run the app
    # -----------------------------------------------------------------------
    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    app = ThetaCodeApp()
    app.run()


if __name__ == "__main__":
    main()
