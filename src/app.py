"""
ThetaCode – Flet desktop GUI
Run with:  flet run src/app.py
           (or: cd src && python app.py)
"""
import atexit
import os
import sys
import threading
from pathlib import Path
from dotenv import load_dotenv
import flet as ft

load_dotenv(Path(__file__).parent.parent / '.env')

# Ensure the src package is importable when running as `flet run app.py`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from storage import Storage                        # noqa: E402
from main import Project, ThetaCode, Chat          # noqa: E402
from llm import get_llm                            # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_MODEL = "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"

# Panel widths
SIDE_PANEL_WIDTH = 210


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
def _msg_bgcolor(role: str) -> str:
    return {
        "user": ft.Colors.PRIMARY_CONTAINER,
        "assistant": ft.Colors.SECONDARY_CONTAINER,
    }.get(role, ft.Colors.SURFACE_CONTAINER)


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
# Message bubble widget
# ---------------------------------------------------------------------------
def build_message_bubble(msg: dict) -> ft.Control:
    role = msg.get("role", "")
    label = _role_label(msg)
    text = _display_content(msg)
    bg = _msg_bgcolor(role)
    thinking = msg.get("thinking", "") or ""
    cost = msg.get("cost", 0.0) or 0.0

    header = ft.Text(
        label,
        size=11,
        weight=ft.FontWeight.BOLD,
        color=ft.Colors.ON_SURFACE_VARIANT,
    )

    # Optional thinking block (collapsed by default via a smaller secondary text)
    thinking_ctrl = None
    if thinking:
        thinking_ctrl = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Text(
                        "🧠 Thinking (click to expand)",
                        size=11,
                        italic=True,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                    ft.Text(
                        thinking,
                        size=11,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                        selectable=True,
                    ),
                ],
                tight=True,
                spacing=2,
            ),
            bgcolor=ft.Colors.SURFACE_CONTAINER,
            border_radius=4,
            padding=6,
            visible=False,  # hidden by default; toggled below
            data="thinking_body",
        )

        def toggle_thinking(e, tc=thinking_ctrl):
            tc.visible = not tc.visible
            tc.update()

        thinking_header = ft.GestureDetector(
            content=ft.Text(
                "🧠 Show / hide thinking",
                size=11,
                italic=True,
                color=ft.Colors.TERTIARY,
            ),
            on_tap=toggle_thinking,
        )
    else:
        thinking_header = None

    body_text = ft.Text(text, selectable=True, size=14)

    cost_text = (
        ft.Text(f"${cost:.6f}", size=10, color=ft.Colors.ON_SURFACE_VARIANT)
        if cost > 0
        else None
    )

    col_controls: list[ft.Control] = [header]
    if thinking_header:
        col_controls.append(thinking_header)
    if thinking_ctrl:
        col_controls.append(thinking_ctrl)
    col_controls.append(body_text)
    if cost_text:
        col_controls.append(cost_text)

    return ft.Container(
        content=ft.Column(controls=col_controls, spacing=4, tight=True),
        bgcolor=bg,
        border_radius=8,
        padding=ft.Padding(left=10, top=8, right=10, bottom=8),
    )


# ---------------------------------------------------------------------------
# Main Flet application
# ---------------------------------------------------------------------------
async def main(page: ft.Page) -> None:
    page.title = "ThetaCode"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 0
    page.spacing = 0

    storage = Storage()

    # ---- Mutable app state (plain dict avoids nonlocal boilerplate) --------
    state: dict = {
        "project_id": None,
        "chat_id": None,
        "theta_code": None,   # ThetaCode | None
        "chat": None,         # Chat | None
        "is_thinking": False,
    }

    # ---- Cleanup on process exit -------------------------------------------
    def _cleanup():
        tc: ThetaCode | None = state.get("theta_code")
        if tc:
            try:
                tc.stop_docker()
            except Exception:
                pass

    atexit.register(_cleanup)

    # -----------------------------------------------------------------------
    # Controls that are referenced from multiple places
    # -----------------------------------------------------------------------

    messages_list = ft.ListView(
        expand=True,
        spacing=8,
        auto_scroll=True,
        padding=12,
    )

    thinking_row = ft.Row(
        controls=[
            ft.ProgressRing(width=16, height=16, stroke_width=2),
            ft.Text(
                "AI is thinking…",
                italic=True,
                size=13,
                color=ft.Colors.ON_SURFACE_VARIANT,
            ),
        ],
        visible=False,
        spacing=8,
    )

    msg_input = ft.TextField(
        hint_text="Type a message… (Enter to send, Shift+Enter for new line)",
        expand=True,
        min_lines=1,
        max_lines=5,
        shift_enter=True,
        filled=True,
        disabled=True,
        border_radius=8,
    )

    model_field = ft.TextField(
        value=DEFAULT_MODEL,
        hint_text="openrouter/<provider>/<model>",
        width=340,
        filled=True,
        border_radius=8,
        dense=True,
    )

    cost_text = ft.Text(
        "Cost: $0.0000",
        size=12,
        color=ft.Colors.ON_SURFACE_VARIANT,
    )

    docker_status = ft.Text(
        "",
        size=11,
        italic=True,
        color=ft.Colors.ON_SURFACE_VARIANT,
    )

    projects_col = ft.Column(
        controls=[],
        spacing=2,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )

    chats_col = ft.Column(
        controls=[],
        spacing=2,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )

    # -----------------------------------------------------------------------
    # File picker (for project folder selection)
    # -----------------------------------------------------------------------
    file_picker = ft.FilePicker()
    page.services.append(file_picker)

    # -----------------------------------------------------------------------
    # Message helpers
    # -----------------------------------------------------------------------

    def _add_bubble(msg: dict) -> None:
        """Append a bubble to the list and refresh the page."""
        if msg.get("role") == "system":
            return
        messages_list.controls.append(build_message_bubble(msg))
        page.update()

    def _persist_and_show(msg: dict) -> None:
        """Persist a message to storage and add it to the UI."""
        chat_id = state["chat_id"]
        role = msg.get("role", "")
        if role == "system":
            return
        if chat_id:
            storage.append_message(
                chat_id=chat_id,
                role=role,
                content=msg.get("content", ""),
                thinking=msg.get("thinking", "") or "",
                cost=msg.get("cost", 0.0) or 0.0,
                llm_model=msg.get("llm", "") or "",
            )
        _add_bubble(msg)
        # Update cost counter
        chat_obj: Chat | None = state["chat"]
        if chat_obj:
            cost_text.value = f"Cost: ${chat_obj.get_cost():.4f}"
            cost_text.update()

    # -----------------------------------------------------------------------
    # Send message
    # -----------------------------------------------------------------------

    def _do_send() -> None:
        text = msg_input.value.strip()
        if not text or state["is_thinking"] or not state["chat"]:
            return

        msg_input.value = ""
        msg_input.disabled = True
        state["is_thinking"] = True
        thinking_row.visible = True
        page.update()

        chat_obj: Chat = state["chat"]
        llm_str = (model_field.value or DEFAULT_MODEL).strip()

        try:
            llm = get_llm(llm_str)
        except ValueError as exc:
            _add_bubble({
                "role": "assistant",
                "content": f"[Configuration error] {exc}\n\nPlease set a valid model name above.",
                "thinking": "",
                "cost": 0.0,
            })
            state["is_thinking"] = False
            thinking_row.visible = False
            msg_input.disabled = False
            page.update()
            return

        def _worker() -> None:
            try:
                chat_obj.send_message(text, llm, on_new_message=_persist_and_show)
            except Exception as exc:
                _persist_and_show({
                    "role": "assistant",
                    "content": f"[Runtime error] {exc}",
                    "thinking": "",
                    "cost": 0.0,
                })
            finally:
                state["is_thinking"] = False
                thinking_row.visible = False
                msg_input.disabled = (state["chat"] is None)
                page.update()

        threading.Thread(target=_worker, daemon=True).start()

    msg_input.on_submit = lambda e: _do_send()

    def _send_click(e):
        _do_send()

    # -----------------------------------------------------------------------
    # Project list
    # -----------------------------------------------------------------------

    def refresh_projects() -> None:
        projects_col.controls.clear()
        for proj in storage.get_projects():
            pid = proj["id"]
            selected = pid == state["project_id"]

            def _make_select_proj(pid=pid):
                def _h(e):
                    _select_project(pid)
                return _h

            def _make_delete_proj(pid=pid):
                def _h(e):
                    _confirm_delete_project(pid)
                return _h

            tile = ft.Container(
                content=ft.Row(
                    controls=[
                        ft.Text(
                            proj["name"],
                            expand=True,
                            size=13,
                            weight=ft.FontWeight.BOLD if selected else ft.FontWeight.NORMAL,
                            overflow=ft.TextOverflow.ELLIPSIS,
                            max_lines=1,
                        ),
                        ft.IconButton(
                            icon=ft.Icons.DELETE_OUTLINE,
                            icon_size=14,
                            tooltip="Delete project",
                            on_click=_make_delete_proj(),
                        ),
                    ],
                    tight=True,
                    spacing=0,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=ft.Padding(left=8, top=6, right=4, bottom=6),
                border_radius=6,
                bgcolor=ft.Colors.PRIMARY_CONTAINER if selected else None,
                on_click=_make_select_proj(),
                ink=True,
            )
            projects_col.controls.append(tile)
        page.update()

    def _select_project(project_id: int) -> None:
        if state["project_id"] == project_id:
            return
        # Tear down old docker
        old_tc: ThetaCode | None = state["theta_code"]
        if old_tc:
            threading.Thread(target=old_tc.stop_docker, daemon=True).start()
            state["theta_code"] = None

        state["project_id"] = project_id
        state["chat_id"] = None
        state["chat"] = None
        messages_list.controls.clear()
        cost_text.value = "Cost: $0.0000"
        docker_status.value = ""
        msg_input.disabled = True
        refresh_projects()
        refresh_chats()

    def _confirm_delete_project(project_id: int) -> None:
        def _confirm(e):
            page.pop_dialog()
            if state["project_id"] == project_id:
                old_tc: ThetaCode | None = state["theta_code"]
                if old_tc:
                    threading.Thread(target=old_tc.stop_docker, daemon=True).start()
                    state["theta_code"] = None
                state["project_id"] = None
                state["chat_id"] = None
                state["chat"] = None
                messages_list.controls.clear()
                msg_input.disabled = True
                docker_status.value = ""
                refresh_chats()
            storage.delete_project(project_id)
            refresh_projects()

        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Delete project?"),
            content=ft.Text("All chats and messages will be permanently deleted."),
            actions=[
                ft.TextButton("Cancel", on_click=lambda e: page.pop_dialog()),
                ft.FilledButton("Delete", on_click=_confirm),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        ))

    # ---- New-project dialog ------------------------------------------------

    _np_name = ft.TextField(label="Project name", autofocus=True, border_radius=8)
    _np_path = ft.TextField(
        label="Project path",
        expand=True,
        hint_text="/home/user/myproject",
        border_radius=8,
    )

    async def _browse_folder(e) -> None:
        path = await file_picker.get_directory_path(dialog_title="Select project folder")
        if path:
            _np_path.value = path
            _np_path.error_text = None
            page.update()

    def _create_project(e) -> None:
        name = (_np_name.value or "").strip()
        path = (_np_path.value or "").strip()
        error = False
        if not name:
            _np_name.error_text = "Name is required"
            error = True
        else:
            _np_name.error_text = None
        if not path:
            _np_path.error_text = "Path is required"
            error = True
        elif not Path(path).exists():
            _np_path.error_text = "Path does not exist"
            error = True
        else:
            _np_path.error_text = None
        if error:
            page.update()
            return
        try:
            pid = storage.create_project(name, path)
        except Exception as exc:
            _np_name.error_text = str(exc)
            page.update()
            return
        _np_name.value = ""
        _np_path.value = ""
        _np_name.error_text = None
        _np_path.error_text = None
        page.pop_dialog()
        refresh_projects()
        _select_project(pid)

    _np_name.on_submit = _create_project

    def _open_new_project_dialog(e) -> None:
        _np_name.value = ""
        _np_path.value = ""
        _np_name.error_text = None
        _np_path.error_text = None
        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("New Project"),
            content=ft.Column(
                controls=[
                    _np_name,
                    ft.Row(
                        controls=[
                            _np_path,
                            ft.IconButton(
                                icon=ft.Icons.FOLDER_OPEN,
                                tooltip="Browse",
                                on_click=_browse_folder,
                            ),
                        ],
                        spacing=4,
                    ),
                ],
                tight=True,
                spacing=12,
                width=420,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=lambda e: page.pop_dialog()),
                ft.FilledButton("Create", on_click=_create_project),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        ))

    # -----------------------------------------------------------------------
    # Chat list
    # -----------------------------------------------------------------------

    def refresh_chats() -> None:
        chats_col.controls.clear()
        if state["project_id"] is None:
            page.update()
            return
        for ch in storage.get_chats(state["project_id"]):
            cid = ch["id"]
            selected = cid == state["chat_id"]

            def _make_select_chat(cid=cid):
                def _h(e):
                    _select_chat(cid)
                return _h

            def _make_delete_chat(cid=cid):
                def _h(e):
                    _confirm_delete_chat(cid)
                return _h

            tile = ft.Container(
                content=ft.Row(
                    controls=[
                        ft.Text(
                            ch["name"],
                            expand=True,
                            size=13,
                            weight=ft.FontWeight.BOLD if selected else ft.FontWeight.NORMAL,
                            overflow=ft.TextOverflow.ELLIPSIS,
                            max_lines=1,
                        ),
                        ft.IconButton(
                            icon=ft.Icons.DELETE_OUTLINE,
                            icon_size=14,
                            tooltip="Delete chat",
                            on_click=_make_delete_chat(),
                        ),
                    ],
                    tight=True,
                    spacing=0,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=ft.Padding(left=8, top=6, right=4, bottom=6),
                border_radius=6,
                bgcolor=ft.Colors.SECONDARY_CONTAINER if selected else None,
                on_click=_make_select_chat(),
                ink=True,
            )
            chats_col.controls.append(tile)
        page.update()

    def _select_chat(chat_id: int) -> None:
        if state["chat_id"] == chat_id:
            return

        state["chat_id"] = chat_id
        state["chat"] = None
        msg_input.disabled = True
        messages_list.controls.clear()
        docker_status.value = "Starting Docker environment…"
        cost_text.value = "Cost: $0.0000"
        refresh_chats()
        page.update()

        proj_row = storage.get_project(state["project_id"])
        if not proj_row:
            return

        project = Project.from_path(proj_row["name"], proj_row["path"])

        # Restore conversation from storage
        stored_msgs = storage.get_messages(chat_id)

        def _init_docker() -> None:
            # Re-use an existing running ThetaCode if it's for the same project
            tc: ThetaCode | None = state["theta_code"]
            if tc is None:
                try:
                    tc = ThetaCode()
                    tc.set_project(project)
                    state["theta_code"] = tc
                except Exception as exc:
                    docker_status.value = f"Docker init error: {exc}"
                    page.update()
                    return

            if not tc._running:
                try:
                    docker_status.value = "Building / starting Docker…"
                    page.update()
                    tc.start_docker(recreate_venvs=False)
                except Exception as exc:
                    docker_status.value = f"Docker start error: {exc}"
                    page.update()
                    return

            # Only proceed if this chat is still the active one
            if state["chat_id"] != chat_id:
                return

            chat_obj = Chat(project, tc)
            chat_obj.restore_messages(stored_msgs)
            state["chat"] = chat_obj

            cost_text.value = f"Cost: ${chat_obj.get_cost():.4f}"
            docker_status.value = "Docker ready ✓"

            # Show stored messages in the UI
            messages_list.controls.clear()
            for msg in stored_msgs:
                if msg["role"] == "system":
                    continue
                _add_bubble({
                    "role": msg["role"],
                    "content": msg["content"],
                    "thinking": msg.get("thinking", ""),
                    "cost": msg.get("cost", 0.0),
                    "llm": msg.get("llm_model", ""),
                })

            msg_input.disabled = False
            page.update()

        threading.Thread(target=_init_docker, daemon=True).start()

    def _confirm_delete_chat(chat_id: int) -> None:
        def _confirm(e) -> None:
            page.pop_dialog()
            if state["chat_id"] == chat_id:
                state["chat_id"] = None
                state["chat"] = None
                messages_list.controls.clear()
                msg_input.disabled = True
                cost_text.value = "Cost: $0.0000"
                docker_status.value = ""
            storage.delete_chat(chat_id)
            refresh_chats()
            page.update()

        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("Delete chat?"),
            content=ft.Text("All messages in this chat will be permanently deleted."),
            actions=[
                ft.TextButton("Cancel", on_click=lambda e: page.pop_dialog()),
                ft.FilledButton("Delete", on_click=_confirm),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        ))

    # ---- New-chat dialog ---------------------------------------------------

    _nc_name = ft.TextField(label="Chat name", autofocus=True, border_radius=8)

    def _create_chat(e) -> None:
        name = (_nc_name.value or "").strip()
        if not name:
            _nc_name.error_text = "Name is required"
            _nc_name.update()
            return
        if state["project_id"] is None:
            return
        cid = storage.create_chat(state["project_id"], name)
        _nc_name.value = ""
        _nc_name.error_text = None
        page.pop_dialog()
        refresh_chats()
        _select_chat(cid)

    _nc_name.on_submit = _create_chat

    def _open_new_chat_dialog(e) -> None:
        if state["project_id"] is None:
            page.show_dialog(ft.AlertDialog(
                modal=True,
                title=ft.Text("No project selected"),
                content=ft.Text("Please select or create a project first."),
                actions=[ft.TextButton("OK", on_click=lambda e: page.pop_dialog())],
            ))
            return
        _nc_name.value = ""
        _nc_name.error_text = None
        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("New Chat"),
            content=ft.Column(
                controls=[_nc_name],
                tight=True,
                spacing=12,
                width=300,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=lambda e: page.pop_dialog()),
                ft.FilledButton("Create", on_click=_create_chat),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        ))

    # -----------------------------------------------------------------------
    # Build the page layout
    # -----------------------------------------------------------------------

    # --- Left panel: projects -----------------------------------------------
    left_panel = ft.Container(
        width=SIDE_PANEL_WIDTH,
        expand=False,
        bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
        content=ft.Column(
            controls=[
                ft.Container(
                    content=ft.Row(
                        controls=[
                            ft.Text(
                                "Projects",
                                size=14,
                                weight=ft.FontWeight.BOLD,
                                expand=True,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.ADD,
                                tooltip="New project",
                                on_click=_open_new_project_dialog,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.Padding(left=12, top=10, right=4, bottom=4),
                ),
                ft.Divider(height=1),
                ft.Container(
                    content=projects_col,
                    expand=True,
                    padding=ft.Padding(left=4, top=4, right=4, bottom=4),
                ),
            ],
            spacing=0,
            expand=True,
        ),
    )

    # --- Middle panel: chats ------------------------------------------------
    middle_panel = ft.Container(
        width=SIDE_PANEL_WIDTH,
        expand=False,
        bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
        content=ft.Column(
            controls=[
                ft.Container(
                    content=ft.Row(
                        controls=[
                            ft.Text(
                                "Chats",
                                size=14,
                                weight=ft.FontWeight.BOLD,
                                expand=True,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.ADD,
                                tooltip="New chat",
                                on_click=_open_new_chat_dialog,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.Padding(left=12, top=10, right=4, bottom=4),
                ),
                ft.Divider(height=1),
                ft.Container(
                    content=chats_col,
                    expand=True,
                    padding=ft.Padding(left=4, top=4, right=4, bottom=4),
                ),
            ],
            spacing=0,
            expand=True,
        ),
    )

    # --- Right panel: messages + input --------------------------------------
    right_panel = ft.Container(
        expand=True,
        bgcolor=ft.Colors.SURFACE,
        content=ft.Column(
            controls=[
                # Messages area
                ft.Container(content=messages_list, expand=True),
                # Thinking indicator
                ft.Container(
                    content=thinking_row,
                    padding=ft.Padding(left=12, top=4, right=12, bottom=0),
                    visible=True,
                ),
                ft.Divider(height=1),
                # Status + model row
                ft.Container(
                    content=ft.Row(
                        controls=[
                            docker_status,
                            ft.Container(expand=True),
                            ft.Text("Model:", size=12),
                            model_field,
                            cost_text,
                        ],
                        spacing=8,
                        alignment=ft.MainAxisAlignment.START,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.Padding(left=12, top=6, right=12, bottom=6),
                ),
                # Input row
                ft.Container(
                    content=ft.Row(
                        controls=[
                            msg_input,
                            ft.IconButton(
                                icon=ft.Icons.SEND_ROUNDED,
                                tooltip="Send (Enter)",
                                on_click=_send_click,
                            ),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.END,
                    ),
                    padding=ft.Padding(left=12, top=0, right=12, bottom=12),
                ),
            ],
            spacing=0,
            expand=True,
        ),
    )

    # --- Assemble -----------------------------------------------------------
    page.add(
        ft.Row(
            controls=[
                left_panel,
                ft.VerticalDivider(width=1),
                middle_panel,
                ft.VerticalDivider(width=1),
                right_panel,
            ],
            spacing=0,
            expand=True,
        )
    )

    # Initial data load
    refresh_projects()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ft.run(main)
else:
    # Also works when loaded by `flet run app.py`
    ft.run(main)
