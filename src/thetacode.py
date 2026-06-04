import asyncio
import os

import flet as ft

from main import Project, ThetaCode, Chat
from llm import get_llm
from persistence import Database


class MessageBubble(ft.Column):
    def __init__(self, text: str, is_user: bool = False, thinking: str = ""):
        super().__init__(spacing=2)
        self.is_user = is_user
        self.thinking = thinking
        self.thinking_container = None
        self.thinking_btn = None

        self.text_control = ft.Text(text, selectable=True, no_wrap=False)

        message_container = ft.Container(
            content=self.text_control,
            bgcolor=ft.Colors.BLUE_100 if is_user else ft.Colors.GREY_100,
            padding=12,
            border_radius=ft.BorderRadius.all(12),
        )

        if is_user:
            row = ft.Row([ft.Container(expand=True), message_container], wrap=False)
        else:
            children = [message_container]
            if thinking:
                self.thinking_control = ft.Text(
                    thinking,
                    size=11,
                    italic=True,
                    color=ft.Colors.GREY_500,
                    selectable=True,
                    no_wrap=False,
                )
                self.thinking_container = ft.Container(
                    content=self.thinking_control,
                    padding=ft.Padding.only(left=12, right=12, top=4, bottom=4),
                    visible=False,
                )
                self.thinking_btn = ft.IconButton(
                    icon=ft.Icons.LIGHTBULB_OUTLINE,
                    icon_size=16,
                    tooltip="Show/hide thinking",
                    on_click=self.toggle_thinking,
                )
                children.append(
                    ft.Row([
                        self.thinking_btn,
                        ft.Text("Thinking", size=11, color=ft.Colors.GREY_500),
                    ], spacing=0)
                )
                children.append(self.thinking_container)
            row = ft.Row(
                [ft.Column(children, spacing=0, tight=True), ft.Container(expand=True)],
                wrap=False,
            )
        self.controls = [row]  # type: ignore[assignment]

    def toggle_thinking(self, _e):
        if self.thinking_container:
            self.thinking_container.visible = not self.thinking_container.visible
            _e.control.icon = (
                ft.Icons.LIGHTBULB
                if self.thinking_container.visible
                else ft.Icons.LIGHTBULB_OUTLINE
            )
            self.update()

    def update_text(self, text: str, thinking: str = ""):
        self.text_control.value = text
        if not self.is_user and thinking and self.thinking_control:
            self.thinking_control.value = thinking
        self.update()


class ThetaCodeUI:
    def __init__(self, page: ft.Page):
        self.page = page
        self.page.title = "ThetaCode"
        self.page.theme_mode = ft.ThemeMode.SYSTEM
        self.page.window_width = 1280
        self.page.window_height = 800

        self.db = Database()
        self.db.init_settings_table()

        self.theta_code: ThetaCode | None = None
        self.current_project_id: int | None = None
        self.current_project: Project | None = None
        self.current_chat_id: int | None = None
        self.current_chat_obj: Chat | None = None
        self._running_task: asyncio.Task | None = None

        # Pre-declare UI attributes set in build_ui to satisfy linter
        self.projects_list: ft.Column | None = None
        self.sidebar_header: ft.Text | None = None
        self.new_project_btn: ft.Button | None = None
        self.sidebar: ft.Column | None = None
        self.main_content: ft.Column | None = None
        self.messages_list: ft.ListView | None = None
        self.chats_list_container: ft.Column | None = None
        self.message_input: ft.TextField | None = None
        self.send_button: ft.IconButton | None = None
        self.model_dropdown: ft.Dropdown | None = None
        self.new_chat_btn: ft.IconButton | None = None
        self.back_to_project_btn: ft.IconButton | None = None
        self.cost_text: ft.Text | None = None
        self.input_row: ft.Container | None = None
        self.file_picker: ft.FilePicker | None = None
        self._new_project_name: ft.TextField | None = None
        self._new_project_path: ft.TextField | None = None
        self._new_chat_title: ft.TextField | None = None
        self._settings_api_key: ft.TextField | None = None
        self._settings_model: ft.TextField | None = None

        self.build_ui()
        self.load_projects()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def build_ui(self):
        # ---- Sidebar --------------------------------------------------
        self.projects_list = ft.Column(scroll=ft.ScrollMode.AUTO, expand=True, spacing=2)
        self.sidebar_header = ft.Text("Projects", size=18, weight=ft.FontWeight.BOLD)
        self.new_project_btn = ft.Button(
            "New Project",
            icon=ft.Icons.ADD,
            on_click=self.show_new_project_dialog,
        )
        self.sidebar = ft.Column(
            [
                self.sidebar_header,
                ft.Divider(height=1),
                self.projects_list,
                ft.Container(
                    self.new_project_btn,
                    padding=10,
                    alignment=ft.alignment.Alignment(0, 0),
                ),
            ],
            width=260,
            tight=True,
        )

        # ---- Main area ------------------------------------------------
        self.main_content = ft.Column(expand=True)
        self.messages_list = ft.ListView(
            expand=True, spacing=10, auto_scroll=True, padding=20
        )
        self.chats_list_container = ft.Column(expand=True, spacing=5)

        # ---- Input bar ------------------------------------------------
        self.message_input = ft.TextField(
            hint_text="Type a message...",
            expand=True,
            multiline=True,
            min_lines=1,
            max_lines=5,
            on_submit=self.on_send,
            disabled=True,
        )
        self.send_button = ft.IconButton(
            icon=ft.Icons.SEND,
            tooltip="Send",
            on_click=self.on_send,
            disabled=True,
        )
        self.model_dropdown = ft.Dropdown(
            value="OpenRouter/openrouter/auto",
            options=[
                ft.dropdown.Option(
                    "OpenRouter/openrouter/auto", "Auto (Best for prompt)"
                ),
                ft.dropdown.Option(
                    "OpenRouter/anthropic/claude-sonnet-4-20250514",
                    "Claude 4 Sonnet",
                ),
                ft.dropdown.Option(
                    "OpenRouter/anthropic/claude-opus-4-20250514",
                    "Claude 4 Opus",
                ),
                ft.dropdown.Option(
                    "OpenRouter/google/gemini-2.5-pro-preview-03-25",
                    "Gemini 2.5 Pro",
                ),
                ft.dropdown.Option("OpenRouter/openai/gpt-4o", "GPT-4o"),
            ],
            width=280,
        )
        self.new_chat_btn = ft.IconButton(
            icon=ft.Icons.ADD_COMMENT,
            tooltip="New chat",
            on_click=self.show_new_chat_dialog,
            visible=False,
        )
        self.back_to_project_btn = ft.IconButton(
            icon=ft.Icons.ARROW_BACK,
            tooltip="Back to project",
            on_click=self.back_to_project,
            visible=False,
        )
        self.cost_text = ft.Text("")
        self.input_row = ft.Container(
            content=ft.Row(
                [
                    self.back_to_project_btn,
                    self.new_chat_btn,
                    self.model_dropdown,
                    self.message_input,
                    self.send_button,
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=15, vertical=10),
            border=ft.Border.only(top=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
            visible=False,
        )

        # ---- AppBar ---------------------------------------------------
        self.page.appbar = ft.AppBar(
            title=ft.Text("ThetaCode"),
            actions=[
                self.cost_text,
                ft.IconButton(
                    icon=ft.Icons.SETTINGS,
                    tooltip="Settings",
                    on_click=self.show_settings_dialog,
                ),
            ],
        )

        # ---- File picker ----------------------------------------------
        self.file_picker = ft.FilePicker()
        self.page.services.append(self.file_picker)

        # ---- Layout ---------------------------------------------------
        self.page.add(
            ft.Row(
                [
                    self.sidebar,
                    ft.VerticalDivider(width=1, color=ft.Colors.OUTLINE_VARIANT),
                    ft.Column(
                        [self.main_content, self.input_row],
                        expand=True,
                        tight=True,
                    ),
                ],
                expand=True,
            )
        )

        self.show_welcome()

    # ------------------------------------------------------------------
    # Navigation / Views
    # ------------------------------------------------------------------
    def show_welcome(self):
        self.main_content.controls = [
            ft.Column(
                [
                    ft.Text("Welcome to ThetaCode", size=32, weight=ft.FontWeight.BOLD),
                    ft.Text(
                        "Select a project from the sidebar or create a new one.",
                        size=16,
                    ),
                    ft.Button(
                        "Create New Project",
                        icon=ft.Icons.ADD,
                        on_click=self.show_new_project_dialog,
                    ),
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                expand=True,
            )
        ]
        self.input_row.visible = False
        self.page.update()

    def show_project_view(self):
        if self.current_project_id is None:
            return
        chats = self.db.get_chats(self.current_project_id)
        rows = []
        for chat in chats:
            delete_btn = ft.IconButton(
                icon=ft.Icons.DELETE_OUTLINE,
                icon_size=18,
                tooltip="Delete chat",
                on_click=lambda _, cid=chat["id"]: self.delete_chat(cid),
            )
            tile = ft.ListTile(
                title=ft.Text(chat["title"]),
                subtitle=ft.Text(chat["created_at"][:19].replace("T", " ")),
                trailing=delete_btn,
                on_click=lambda _, cid=chat["id"]: self.load_chat(cid),
            )
            rows.append(tile)

        self.chats_list_container.controls = rows

        project_name = ""
        if self.current_project is not None:
            project_name = self.current_project.name

        self.main_content.controls = [
            ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text(
                                project_name,
                                size=24,
                                weight=ft.FontWeight.BOLD,
                                expand=True,
                            ),
                            ft.Button(
                                "New Chat",
                                icon=ft.Icons.ADD_COMMENT,
                                on_click=self.show_new_chat_dialog,
                            ),
                            ft.Button(
                                "Close Project",
                                icon=ft.Icons.CLOSE,
                                on_click=self.close_project,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Divider(),
                    ft.Text("Chats", size=18, weight=ft.FontWeight.BOLD),
                    self.chats_list_container,
                ],
                expand=True,
            )
        ]
        self.input_row.visible = False
        self.page.update()

    def show_chat_view(self):
        self.main_content.controls = [self.messages_list]
        self.input_row.visible = True
        self.new_chat_btn.visible = True
        self.back_to_project_btn.visible = True
        self.message_input.disabled = False
        self.send_button.disabled = False
        self.page.update()

    def back_to_project(self, _e=None):
        self.current_chat_id = None
        self.current_chat_obj = None
        self.messages_list.controls.clear()
        self.show_project_view()

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------
    def load_projects(self):
        self.projects_list.controls.clear()
        for proj in self.db.list_projects():
            is_selected = proj["id"] == self.current_project_id
            tile = ft.ListTile(
                title=ft.Text(
                    proj["name"],
                    weight=ft.FontWeight.BOLD if is_selected else ft.FontWeight.NORMAL,
                ),
                selected=is_selected,
                bgcolor=ft.Colors.PRIMARY_CONTAINER if is_selected else None,
                on_click=lambda _, pid=proj["id"]: self.select_project(pid),
            )
            delete_btn = ft.IconButton(
                icon=ft.Icons.DELETE_OUTLINE,
                icon_size=18,
                tooltip="Delete project",
                on_click=lambda _, pid=proj["id"]: self.confirm_delete_project(pid),
            )
            self.projects_list.controls.append(
                ft.Row([ft.Container(tile, expand=True), delete_btn], spacing=0)
            )
        self.page.update()

    def select_project(self, project_id: int):
        if self.current_project_id == project_id:
            self.show_project_view()
            return

        # stop previous docker if any
        if self.theta_code is not None:
            try:
                self.theta_code.stop_docker()
            except Exception:  # noqa
                pass

        proj = self.db.get_project(project_id)
        if not proj:
            return

        self.db.update_project_last_opened(project_id)
        self.current_project_id = project_id
        self.current_project = Project(proj["name"], proj["path"])
        self.current_chat_id = None
        self.current_chat_obj = None
        self.messages_list.controls.clear()

        # start docker
        self.theta_code = ThetaCode()
        self.theta_code.project = self.current_project
        self.page.run_task(self._start_docker_async)

    async def _start_docker_async(self):
        def _start():
            if self.theta_code is not None:
                self.theta_code.start_docker()

        # type: ignore[arg-type]  # run_in_executor doesn't accept plain functions in stubs
        await asyncio.get_event_loop().run_in_executor(None, _start)
        self.load_projects()
        self.show_project_view()

    def close_project(self, _e=None):
        if self.theta_code is not None:
            try:
                self.theta_code.stop_docker()
            except Exception:  # noqa
                pass
            self.theta_code = None
        self.current_project_id = None
        self.current_project = None
        self.current_chat_id = None
        self.current_chat_obj = None
        self.messages_list.controls.clear()
        self.load_projects()
        self.show_welcome()

    def confirm_delete_project(self, project_id: int):
        def on_confirm(_):
            self.delete_project(project_id)
            dlg.open = False
            self.page.update()

        def on_cancel(_):
            dlg.open = False
            self.page.update()

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Delete Project"),
            content=ft.Text(
                "Delete this project and all its chats? This cannot be undone."
            ),
            actions=[
                ft.TextButton("Cancel", on_click=on_cancel),
                ft.TextButton(
                    "Delete",
                    on_click=on_confirm,
                    style=ft.ButtonStyle(color=ft.Colors.ERROR),
                ),
            ],
        )
        self.page.dialog = dlg
        dlg.open = True
        self.page.update()

    def delete_project(self, project_id: int):
        if self.current_project_id == project_id:
            self.close_project()
        self.db.delete_project(project_id)
        self.load_projects()

    # ------------------------------------------------------------------
    # New-project dialog
    # ------------------------------------------------------------------
    def show_new_project_dialog(self, _e=None):
        self._new_project_name = ft.TextField(label="Project name")
        self._new_project_path = ft.TextField(label="Original path", expand=True)

        async def pick_path(_event):
            path = await self.file_picker.get_directory_path()
            if path:
                self._new_project_path.value = path
                self.page.update()

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Create New Project"),
            content=ft.Column(
                [
                    self._new_project_name,
                    ft.Row([
                        self._new_project_path,
                        ft.IconButton(
                            icon=ft.Icons.FOLDER_OPEN, on_click=pick_path
                        ),
                    ]),
                ],
                tight=True,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: self.close_dialog(dlg)),
                ft.Button("Create", on_click=lambda _: self.create_project(dlg)),
            ],
        )
        self.page.dialog = dlg
        dlg.open = True
        self.page.update()

    def create_project(self, dlg: ft.AlertDialog):
        name = self._new_project_name.value.strip()
        path = self._new_project_path.value.strip()
        if not name or not path:
            return
        try:
            proj = Project.create(name, path)
            project_id = self.db.create_project(name, proj.path)
            dlg.open = False
            self.page.update()
            self.select_project(project_id)
        except Exception as exc:
            dlg.content.controls.append(
                ft.Text(f"Error: {exc}", color=ft.Colors.ERROR)
            )
            self.page.update()

    # ------------------------------------------------------------------
    # Chats
    # ------------------------------------------------------------------
    def show_new_chat_dialog(self, _e=None):
        self._new_chat_title = ft.TextField(label="Chat title", value="New Chat")

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("New Chat"),
            content=self._new_chat_title,
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: self.close_dialog(dlg)),
                ft.Button("Create", on_click=lambda _: self.create_chat(dlg)),
            ],
        )
        self.page.dialog = dlg
        dlg.open = True
        self.page.update()

    def create_chat(self, dlg: ft.AlertDialog):
        title = self._new_chat_title.value.strip() or "New Chat"
        if self.current_project_id is None:
            return
        chat_id = self.db.create_chat(self.current_project_id, title)
        dlg.open = False
        self.page.update()
        self.load_chat(chat_id)

    def load_chat(self, chat_id: int):
        if (
            self.current_chat_obj is not None
            and self._running_task is not None
            and not self._running_task.done()
        ):
            self._running_task.cancel()

        self.current_chat_id = chat_id
        self.messages_list.controls.clear()

        chat = self.db.get_chat(chat_id)
        if not chat:
            return

        if self.current_project is None or self.theta_code is None:
            return

        self.current_chat_obj = Chat(
            self.current_project,
            self.theta_code,
            self.db,
            chat_id,
        )
        self.current_chat_obj.load_history()

        for msg in self.current_chat_obj._conversation:  # noqa: SLF001
            if msg["role"] == "system":
                continue
            if msg["role"] == "user":
                # strip XML wrapper for display
                text = msg["content"]
                if text.startswith("<user_message>"):
                    text = text[len("<user_message>"):]
                    if text.endswith("</user_message>"):
                        text = text[: -len("</user_message>")]
                    text = text.strip()
                self.messages_list.controls.append(MessageBubble(text, is_user=True))
            elif msg["role"] == "assistant":
                self.messages_list.controls.append(
                    MessageBubble(
                        msg["content"],
                        is_user=False,
                        thinking=msg.get("thinking", ""),
                    )
                )

        self.update_cost_label()
        self.show_chat_view()

    def delete_chat(self, chat_id: int):
        def on_confirm(_):
            self.db.delete_chat(chat_id)
            if self.current_chat_id == chat_id:
                self.back_to_project()
            else:
                self.show_project_view()
            dlg.open = False
            self.page.update()

        def on_cancel(_):
            dlg.open = False
            self.page.update()

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Delete Chat"),
            content=ft.Text("Delete this chat? This cannot be undone."),
            actions=[
                ft.TextButton("Cancel", on_click=on_cancel),
                ft.TextButton(
                    "Delete",
                    on_click=on_confirm,
                    style=ft.ButtonStyle(color=ft.Colors.ERROR),
                ),
            ],
        )
        self.page.dialog = dlg
        dlg.open = True
        self.page.update()

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------
    def update_cost_label(self):
        if self.current_chat_obj is not None:
            cost = self.current_chat_obj.get_cost()
            self.cost_text.value = f"Cost: ${cost:.4f}"
        else:
            self.cost_text.value = ""
        self.page.update()

    async def on_send(self, _e):
        text = self.message_input.value.strip()
        if not text or self.current_chat_obj is None:
            return

        self.message_input.value = ""
        self.message_input.disabled = True
        self.send_button.disabled = True
        self.page.update()

        # add user bubble
        self.messages_list.controls.append(MessageBubble(text, is_user=True))
        await self.messages_list.scroll_to(offset=-1, duration=0)
        self.page.update()

        model = self.model_dropdown.value or ""
        try:
            llm = get_llm(model)
        except ValueError as exc:
            self.messages_list.controls.append(
                MessageBubble(f"Error: {exc}", is_user=False)
            )
            self._enable_input()
            return

        self.current_chat_obj.send_message(text, llm)

        assistant_bubble = MessageBubble("", is_user=False, thinking="")
        self.messages_list.controls.append(assistant_bubble)
        self.page.update()

        try:
            async for event in self.current_chat_obj.run_async(llm):
                if event["type"] == "assistant":
                    assistant_bubble.update_text(
                        event["content"], event.get("thinking", "")
                    )
                    await self.messages_list.scroll_to(offset=-1, duration=0)
                elif event["type"] == "tool_call":
                    self.messages_list.controls.append(
                        ft.Container(
                            content=ft.Text(
                                f"  Tool: {event['tool']}",
                                size=12,
                                color=ft.Colors.GREY_600,
                            ),
                            padding=ft.Padding.only(left=20),
                        )
                    )
                    await self.messages_list.scroll_to(offset=-1, duration=0)
                elif event["type"] == "tool_error":
                    self.messages_list.controls.append(
                        ft.Container(
                            content=ft.Text(
                                f"  Tool error: {event['error']}",
                                size=12,
                                color=ft.Colors.ERROR,
                            ),
                            padding=ft.Padding.only(left=20),
                        )
                    )
                elif event["type"] == "ask_user":
                    self.messages_list.controls.append(
                        MessageBubble(
                            f"Agent asks: {event.get('question', '')}",
                            is_user=False,
                        )
                    )
                self.page.update()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.messages_list.controls.append(
                MessageBubble(f"Error during generation: {exc}", is_user=False)
            )

        self.update_cost_label()
        self._enable_input()

    def _enable_input(self):
        self.message_input.disabled = False
        self.send_button.disabled = False
        self.message_input.focus()
        self.page.update()

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def show_settings_dialog(self, _e=None):
        api_key = self.db.get_setting("openrouter_api_key") or os.environ.get(
            "OPENROUTER_API_KEY", ""
        )
        default_model = (
            self.db.get_setting("default_model") or "OpenRouter/openrouter/auto"
        )

        self._settings_api_key = ft.TextField(
            label="OpenRouter API Key",
            value=api_key,
            password=True,
            can_reveal_password=True,
        )
        self._settings_model = ft.TextField(
            label="Default Model", value=default_model
        )

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Settings"),
            content=ft.Column(
                [
                    self._settings_api_key,
                    self._settings_model,
                    ft.Text(
                        "If API Key is empty, OPENROUTER_API_KEY env var is used.",
                        size=12,
                        color=ft.Colors.GREY_500,
                    ),
                ],
                tight=True,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: self.close_dialog(dlg)),
                ft.Button("Save", on_click=lambda _: self.save_settings(dlg)),
            ],
        )
        self.page.dialog = dlg
        dlg.open = True
        self.page.update()

    def save_settings(self, dlg: ft.AlertDialog):
        self.db.set_setting(
            "openrouter_api_key", self._settings_api_key.value.strip()
        )
        self.db.set_setting(
            "default_model", self._settings_model.value.strip()
        )
        if self._settings_model.value.strip():
            self.model_dropdown.value = self._settings_model.value.strip()
        dlg.open = False
        self.page.update()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def close_dialog(self, dlg: ft.AlertDialog):
        dlg.open = False
        self.page.update()

    def on_window_event(self, _e):
        if _e.data == "close":
            if self.theta_code is not None:
                try:
                    self.theta_code.stop_docker()
                except Exception:  # noqa
                    pass
            self.page.window.destroy()


def main(page: ft.Page):
    app = ThetaCodeUI(page)
    page.on_window_event = app.on_window_event


if __name__ == "__main__":
    ft.run(main=main)
