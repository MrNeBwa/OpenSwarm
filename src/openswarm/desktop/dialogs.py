"""Native dialogs for OpenSwarm Desktop Application:
- Native folder chooser (Gtk.FileDialog / Gtk.FileChooserNative)
- Libadwaita Preferences Window
- Keyboard Shortcuts Window (AT-SPI accessible)
- About Dialog
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("openswarm.desktop.dialogs")

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gio", "2.0")
    from gi.repository import Adw, Gio, GLib, Gtk
    HAS_ADW = True
except Exception:
    HAS_ADW = False


def show_folder_chooser(parent: Any, initial_path: str, callback: Callable[[str | None], None]) -> None:
    """Opens the native Linux folder picker dialog."""
    init_p = Path(initial_path).expanduser().resolve() if initial_path else Path.cwd()
    if not init_p.is_dir():
        init_p = Path.cwd()

    try:
        # GTK 4.10+ modern async portal FileDialog
        if hasattr(Gtk, "FileDialog"):
            dialog = Gtk.FileDialog(title="Выберите рабочую папку проекта")
            try:
                gfile = Gio.File.new_for_path(str(init_p))
                dialog.set_initial_folder(gfile)
            except Exception:
                pass

            def _on_finish(source: Any, res: Any) -> None:
                try:
                    folder = source.select_folder_finish(res)
                    path_str = folder.get_path() if folder else None
                    GLib.idle_add(callback, path_str)
                except Exception as exc:
                    logger.debug("FileDialog selection cancelled or failed: %s", exc)
                    GLib.idle_add(callback, None)

            dialog.select_folder(parent, None, _on_finish)
            return
    except Exception as exc:
        logger.debug("Gtk.FileDialog failed, falling back to FileChooserNative: %s", exc)

    # Fallback to Gtk.FileChooserNative
    try:
        native = Gtk.FileChooserNative.new(
            "Выберите рабочую папку проекта",
            parent,
            Gtk.FileChooserAction.SELECT_FOLDER,
            "Выбрать",
            "Отмена",
        )
        try:
            native.set_current_folder(Gio.File.new_for_path(str(init_p)))
        except Exception:
            pass

        def _on_response(_dialog: Any, response_id: int) -> None:
            if response_id == Gtk.ResponseType.ACCEPT:
                gfile = native.get_file()
                path_str = gfile.get_path() if gfile else None
                callback(path_str)
            else:
                callback(None)
            native.destroy()

        native.connect("response", _on_response)
        native.show()
    except Exception as exc:
        logger.error("Folder picker completely failed: %s", exc)
        callback(None)


def show_preferences_window(parent: Any, cfg: Any, on_change: Callable[[str, Any], None] | None = None) -> None:
    """Shows Libadwaita Preferences Window."""
    if not HAS_ADW:
        return

    win = Adw.PreferencesWindow(title="Настройки OpenSwarm", transient_for=parent, modal=True)
    win.set_default_size(680, 560)

    # --- Page 1: Внешний вид и Доступность
    page_ui = Adw.PreferencesPage(title="Интерфейс", icon_name="preferences-desktop-theme-symbolic")
    win.add(page_ui)

    grp_theme = Adw.PreferencesGroup(title="Оформление и тема")
    page_ui.add(grp_theme)

    # Color scheme
    row_theme = Adw.ComboRow(title="Цветовая тема")
    model_theme = Gtk.StringList.new(["Системная (авто)", "Тёмная", "Светлая"])
    row_theme.set_model(model_theme)
    cur_scheme = getattr(cfg, "color_scheme", "default")
    row_theme.set_selected(1 if cur_scheme == "dark" else (2 if cur_scheme == "light" else 0))

    def _on_theme_changed(row: Any, _param: Any) -> None:
        idx = row.get_selected()
        val = "dark" if idx == 1 else ("light" if idx == 2 else "default")
        cfg.color_scheme = val
        cfg.save()
        style_mgr = Adw.StyleManager.get_default()
        if val == "dark":
            style_mgr.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        elif val == "light":
            style_mgr.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        else:
            style_mgr.set_color_scheme(Adw.ColorScheme.DEFAULT)
        if on_change:
            on_change("color_scheme", val)

    row_theme.connect("notify::selected", _on_theme_changed)
    grp_theme.add(row_theme)

    # Accessibility group
    grp_a11y = Adw.PreferencesGroup(title="Специальные возможности и масштаб")
    page_ui.add(grp_a11y)

    row_contrast = Adw.SwitchRow(title="Высокая контрастность (High Contrast)", subtitle="Максимальная читаемость и чёткие границы элементов")
    row_contrast.set_active(getattr(cfg, "high_contrast", False))

    def _on_contrast_toggled(row: Any, _param: Any) -> None:
        val = row.get_active()
        cfg.high_contrast = val
        cfg.save()
        if on_change:
            on_change("high_contrast", val)

    row_contrast.connect("notify::active", _on_contrast_toggled)
    grp_a11y.add(row_contrast)

    row_zoom = Adw.ComboRow(title="Масштаб шрифта и элементов")
    zoom_model = Gtk.StringList.new(["85%", "100% (по умолчанию)", "115%", "130%", "150%"])
    row_zoom.set_model(zoom_model)
    cur_zoom = getattr(cfg, "zoom_level", 1.0)
    zoom_idx = 1
    if cur_zoom <= 0.9:
        zoom_idx = 0
    elif cur_zoom >= 1.4:
        zoom_idx = 4
    elif cur_zoom >= 1.25:
        zoom_idx = 3
    elif cur_zoom >= 1.1:
        zoom_idx = 2
    row_zoom.set_selected(zoom_idx)

    def _on_zoom_changed(row: Any, _param: Any) -> None:
        levels = [0.85, 1.0, 1.15, 1.3, 1.5]
        sel_idx = row.get_selected()
        lvl = levels[sel_idx] if 0 <= sel_idx < len(levels) else 1.0
        cfg.zoom_level = lvl
        cfg.save()
        if on_change:
            on_change("zoom_level", lvl)

    row_zoom.connect("notify::selected", _on_zoom_changed)
    grp_a11y.add(row_zoom)

    # --- Page 2: Выносной ИИ-питомец и Безопасность
    page_pet = Adw.PreferencesPage(title="Питомец", icon_name="emblem-favorite-symbolic")
    win.add(page_pet)

    grp_pet = Adw.PreferencesGroup(title="Питомец-компаньон рабочего стола")
    page_pet.add(grp_pet)

    row_pet_ontop = Adw.SwitchRow(title="Всегда поверх окон (Always on Top)", subtitle="Закреплять плавающего питомца поверх тайловых менеджеров и приложений")
    row_pet_ontop.set_active(getattr(cfg, "pet_ontop", True))

    def _on_pet_ontop_toggled(row: Any, _param: Any) -> None:
        val = row.get_active()
        cfg.pet_ontop = val
        cfg.save()
        if on_change:
            on_change("pet_ontop", val)

    row_pet_ontop.connect("notify::active", _on_pet_ontop_toggled)
    grp_pet.add(row_pet_ontop)

    row_pet_overlay = Adw.SwitchRow(title="Прозрачный фон (Overlay)", subtitle="Безрамочный компактный виджет с плавающими карточками мыслей")
    row_pet_overlay.set_active(getattr(cfg, "pet_overlay", True))

    def _on_pet_overlay_toggled(row: Any, _param: Any) -> None:
        val = row.get_active()
        cfg.pet_overlay = val
        cfg.save()
        if on_change:
            on_change("pet_overlay", val)

    row_pet_overlay.connect("notify::active", _on_pet_overlay_toggled)
    grp_pet.add(row_pet_overlay)

    grp_sec = Adw.PreferencesGroup(title="Безопасность автоматизации")
    page_pet.add(grp_sec)

    row_confirm = Adw.SwitchRow(title="Запрашивать подтверждение для действий в приложениях", subtitle="Ни одно действие на рабочем столе не выполнится без вашего согласия")
    row_confirm.set_active(getattr(cfg, "confirm_desktop_actions", True))

    def _on_confirm_toggled(row: Any, _param: Any) -> None:
        val = row.get_active()
        cfg.confirm_desktop_actions = val
        cfg.save()
        if on_change:
            on_change("confirm_desktop_actions", val)

    row_confirm.connect("notify::active", _on_confirm_toggled)
    grp_sec.add(row_confirm)

    win.present()


def show_shortcuts_window(parent: Any) -> None:
    """Shows keyboard shortcuts and accessibility dialog."""
    try:
        win = Gtk.ShortcutsWindow(transient_for=parent, modal=True)
        sec = Gtk.ShortcutsSection(visible=True, max_columns=2)
        win.add_child(sec)

        # Group 1: Навигация и диалоги
        grp_nav = Gtk.ShortcutsGroup(title="Навигация и чат", visible=True)
        sec.add_group(grp_nav)

        shortcuts_nav = [
            ("Новый чат / диалог", "<Primary>n"),
            ("Выбрать рабочую папку проекта", "<Primary>o"),
            ("Фокус в поле ввода", "<Primary>l"),
            ("Отправить задачу агентам", "Return"),
            ("Принудительный перенос строки", "<Shift>Return"),
            ("Закрыть окно / модалку", "Escape"),
        ]
        for title, accel in shortcuts_nav:
            s = Gtk.ShortcutsShortcut(title=title, accelerator=accel, visible=True)
            grp_nav.add_shortcut(s)

        # Group 2: Доступность и масштаб
        grp_a11y = Gtk.ShortcutsGroup(title="Доступность и масштаб", visible=True)
        sec.add_group(grp_a11y)

        shortcuts_a11y = [
            ("Увеличить масштаб", "<Primary>plus"),
            ("Уменьшить масштаб", "<Primary>minus"),
            ("Сбросить масштаб (100%)", "<Primary>0"),
            ("Режим высокой контрастности", "<Primary><Shift>h"),
            ("Справка по горячим клавишам", "F1"),
        ]
        for title, accel in shortcuts_a11y:
            s = Gtk.ShortcutsShortcut(title=title, accelerator=accel, visible=True)
            grp_a11y.add_shortcut(s)

        # Group 3: Питомец и окна
        grp_pet = Gtk.ShortcutsGroup(title="Питомец и приложение", visible=True)
        sec.add_group(grp_pet)

        shortcuts_pet = [
            ("Включить / скрыть выносного питомца", "<Primary><Shift>p"),
            ("Открыть настройки приложения", "<Primary>comma"),
            ("Полноэкранный режим", "F11"),
            ("Обновить интерфейс", "<Primary>r"),
            ("Закрыть приложение", "<Primary>q"),
        ]
        for title, accel in shortcuts_pet:
            s = Gtk.ShortcutsShortcut(title=title, accelerator=accel, visible=True)
            grp_pet.add_shortcut(s)

        win.present()
    except Exception as exc:
        logger.warning("Could not open native shortcuts window: %s", exc)


def show_about_dialog(parent: Any, version: str) -> None:
    """Shows About OpenSwarm Dialog."""
    try:
        icon_name = "openswarm"
        if HAS_ADW and hasattr(Adw, "AboutDialog"):
            about = Adw.AboutDialog(
                application_name="OpenSwarm",
                application_icon=icon_name,
                developer_name="Google DeepMind OpenSwarm Project",
                version=f"v{version}",
                comments="Автономная мультиагентная среда разработки и ИИ-оркестратор",
                website="https://github.com/google/openswarm",
                issue_url="https://github.com/google/openswarm/issues",
                license_type=Gtk.License.APACHE_2_0,
                copyright="© 2026 OpenSwarm Contributors",
            )
            about.present(parent)
            return
        elif HAS_ADW and hasattr(Adw, "AboutWindow"):
            about = Adw.AboutWindow(
                transient_for=parent,
                modal=True,
                application_name="OpenSwarm",
                application_icon=icon_name,
                developer_name="Google DeepMind OpenSwarm Project",
                version=f"v{version}",
                comments="Автономная мультиагентная среда разработки и ИИ-оркестратор",
                website="https://github.com/google/openswarm",
                issue_url="https://github.com/google/openswarm/issues",
                license_type=Gtk.License.APACHE_2_0,
                copyright="© 2026 OpenSwarm Contributors",
            )
            about.present()
            return
    except Exception as exc:
        logger.debug("Adw.AboutDialog failed: %s", exc)

    # Standard Gtk.AboutDialog fallback
    try:
        dialog = Gtk.AboutDialog(
            transient_for=parent,
            modal=True,
            program_name="OpenSwarm",
            version=version,
            comments="Автономная мультиагентная среда разработки и ИИ-оркестратор",
            website="https://github.com/google/openswarm",
            license_type=Gtk.License.APACHE_2_0,
            copyright="© 2026 OpenSwarm Contributors",
        )
        dialog.present()
    except Exception as exc:
        logger.error("Could not show About dialog: %s", exc)
