import json
import re
import threading
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
import sys
from tkinter import filedialog, messagebox

import customtkinter as ctk

def get_app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent
BASE_DIR = get_app_base_dir()
APP_DIR = BASE_DIR / "app_data"
APP_DIR.mkdir(parents=True, exist_ok=True)
SCHEDULES_FILE = APP_DIR / "schedules.json"
MIN_DATE = date(2011, 1, 1)
DEFAULT_OUTPUT_DIR = (BASE_DIR / "output_pogodaiklimat").resolve()


@dataclass
class ScheduleItem:
    name: str
    station_ids_raw: str
    run_time: str
    range_mode: str
    days_back: int
    output_dir: str
    enabled: bool = True
    last_run_date: str = ""


def parse_station_ids(raw: str) -> list[str] | None:
    raw = raw.strip()
    if not raw:
        return None
    parts = [p.strip() for p in raw.replace(";", ",").replace("\n", ",").split(",")]
    ids = [p for p in parts if p]
    bad = [p for p in ids if not p.isdigit()]
    if bad:
        raise ValueError(f"Некорректные ID станций: {', '.join(bad)}")
    return ids or None


def validate_manual_dates(start_value: str, end_value: str) -> tuple[date, date]:
    try:
        start_date = datetime.strptime(start_value, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("Даты нужно вводить в формате YYYY-MM-DD")

    if start_date < MIN_DATE or end_date < MIN_DATE:
        raise ValueError("Дата не может быть раньше 2011-01-01")
    if start_date > end_date:
        raise ValueError("Дата начала не может быть позже даты конца")

    yesterday = date.today() - timedelta(days=1)
    if end_date > yesterday:
        raise ValueError("Дата конца не может быть позже вчерашнего дня")

    return start_date, end_date


class SchedulerService:
    def __init__(self, append_log, trigger_job, refresh_callback):
        self.append_log = append_log
        self.trigger_job = trigger_job
        self.refresh_callback = refresh_callback
        self.items: list[ScheduleItem] = []
        self._load()

    def _load(self):
        if not SCHEDULES_FILE.exists():
            self.items = []
            return
        try:
            raw = json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
            self.items = [ScheduleItem(**item) for item in raw]
        except Exception as e:
            self.append_log(f"[scheduler] Не удалось загрузить schedules.json: {e}")
            self.items = []

    def save(self):
        SCHEDULES_FILE.write_text(
            json.dumps([asdict(item) for item in self.items], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add(self, item: ScheduleItem):
        self.items.append(item)
        self.save()
        self.refresh_callback()

    def delete(self, index: int):
        del self.items[index]
        self.save()
        self.refresh_callback()

    def toggle(self, index: int, enabled: bool):
        self.items[index].enabled = enabled
        self.save()
        self.refresh_callback()

    def calc_range(self, item: ScheduleItem) -> tuple[date, date]:
        yesterday = date.today() - timedelta(days=1)
        if item.range_mode == "Только вчера":
            return yesterday, yesterday
        days_back = max(1, int(item.days_back))
        start_date = yesterday - timedelta(days=days_back - 1)
        return start_date, yesterday

    def poll(self):
        now = datetime.now()
        now_hm = now.strftime("%H:%M")
        today_str = now.strftime("%Y-%m-%d")

        for item in self.items:
            if not item.enabled:
                continue
            if item.run_time != now_hm:
                continue
            if item.last_run_date == today_str:
                continue

            try:
                start_date, end_date = self.calc_range(item)
                station_ids = parse_station_ids(item.station_ids_raw)
                self.append_log(
                    f"[scheduler] Запуск задачи '{item.name}': "
                    f"станции={station_ids or 'все из CSV'}, "
                    f"период={start_date}..{end_date}, папка={item.output_dir}"
                )
                self.trigger_job(
                    station_ids=station_ids,
                    start_date=start_date,
                    end_date=end_date,
                    output_dir=item.output_dir,
                    job_name=f"schedule:{item.name}",
                )
                item.last_run_date = today_str
                self.save()
                self.refresh_callback()
            except Exception as e:
                self.append_log(f"[scheduler] Ошибка задачи '{item.name}': {e}")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("Meteodana Parser")
        self.geometry("1320x820")
        self.minsize(1120, 720)

        self.edit_schedule_index = None
        self.current_stop_event = None
        self.current_worker_thread = None
        self.current_job_name = None
        self.manual_output_dir = ctk.StringVar(value=str(DEFAULT_OUTPUT_DIR))
        self.sched_output_dir = ctk.StringVar(value=str(DEFAULT_OUTPUT_DIR))

        self.scheduler = SchedulerService(
            append_log=self.append_log,
            trigger_job=self.run_job_in_thread,
            refresh_callback=self.refresh_schedule_list,
        )

        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=2)
        self.grid_rowconfigure(0, weight=1)

        self._build_left_panel()
        self._build_right_panel()
        self.refresh_schedule_list()
        self.append_log("Интерфейс готов. Можно запускать разовый парсинг или настраивать расписание.")
        self.after(10_000, self.scheduler_tick)

    def _build_left_panel(self):
        left = ctk.CTkFrame(self)
        left.grid(row=0, column=0, sticky="nsew", padx=(12, 6), pady=12)
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)

        title = ctk.CTkLabel(left, text="Meteodana Parser", font=ctk.CTkFont(size=24, weight="bold"))
        title.grid(row=0, column=0, sticky="w", padx=16, pady=(16, 8))

        self.tabs = ctk.CTkTabview(left)
        self.tabs.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.tabs.add("Разовый запуск")
        self.tabs.add("Автоматический запуск")

        self._build_manual_tab()
        self._build_schedule_tab()

    def _build_right_panel(self):
        right = ctk.CTkFrame(self)
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 12), pady=12)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        log_title = ctk.CTkLabel(right, text="Лог", font=ctk.CTkFont(size=20, weight="bold"))
        log_title.grid(row=0, column=0, sticky="w", padx=16, pady=(16, 8))

        self.log_box = ctk.CTkTextbox(right, wrap="word")
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))

    def _build_manual_tab(self):
        tab = self.tabs.tab("Разовый запуск")
        tab.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(tab, text="ID станций").grid(row=0, column=0, sticky="w", padx=16, pady=(16, 6))
        self.manual_station_ids = ctk.CTkTextbox(tab, height=90)
        self.manual_station_ids.grid(row=1, column=0, sticky="ew", padx=16)
        self.manual_station_ids.insert("1.0", "")

        ctk.CTkLabel(
            tab,
            text="Через запятую. Оставь пустым, если нужно брать все станции из PDF.",
            text_color="gray70",
        ).grid(row=2, column=0, sticky="w", padx=16, pady=(6, 10))

        dates_frame = ctk.CTkFrame(tab)
        dates_frame.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 12))
        dates_frame.grid_columnconfigure((0, 1), weight=1)

        ctk.CTkLabel(dates_frame, text="Дата начала (YYYY-MM-DD)").grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))
        ctk.CTkLabel(dates_frame, text="Дата конца (YYYY-MM-DD)").grid(row=0, column=1, sticky="w", padx=12, pady=(12, 6))

        self.manual_start = ctk.CTkEntry(dates_frame)
        self.manual_start.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
        self.manual_start.insert(0, "2021-03-01")

        self.manual_end = ctk.CTkEntry(dates_frame)
        self.manual_end.grid(row=1, column=1, sticky="ew", padx=12, pady=(0, 12))
        self.manual_end.insert(0, (date.today() - timedelta(days=1)).strftime("%Y-%m-%d"))

        ctk.CTkLabel(
            tab,
            text="Минимальная дата: 2011-01-01. Максимальная дата: вчерашний день.",
            text_color="gray70",
        ).grid(row=4, column=0, sticky="w", padx=16, pady=(0, 12))

        ctk.CTkLabel(tab, text="Папка сохранения").grid(row=5, column=0, sticky="w", padx=16, pady=(0, 6))

        manual_out_frame = ctk.CTkFrame(tab)
        manual_out_frame.grid(row=6, column=0, sticky="ew", padx=16, pady=(0, 12))
        manual_out_frame.grid_columnconfigure(0, weight=1)

        self.manual_output_entry = ctk.CTkEntry(manual_out_frame, textvariable=self.manual_output_dir)
        self.manual_output_entry.grid(row=0, column=0, sticky="ew", padx=(12, 8), pady=12)

        ctk.CTkButton(
            manual_out_frame,
            text="Выбрать папку",
            command=self.choose_manual_output_dir
        ).grid(row=0, column=1, padx=(0, 12), pady=12)

        buttons_frame = ctk.CTkFrame(tab, fg_color="transparent")
        buttons_frame.grid(row=7, column=0, sticky="w", padx=16, pady=(0, 16))

        run_btn = ctk.CTkButton(buttons_frame, text="Запустить парсинг сейчас", command=self.on_manual_run)
        run_btn.grid(row=0, column=0, padx=(0, 10))

        stop_btn = ctk.CTkButton(
            buttons_frame,
            text="Остановить парсинг",
            fg_color="#8B1E1E",
            hover_color="#A52A2A",
            command=self.stop_current_job,
        )
        stop_btn.grid(row=0, column=1)

    def _build_schedule_tab(self):
        tab = self.tabs.tab("Автоматический запуск")
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(9, weight=1)

        ctk.CTkLabel(tab, text="Название задачи").grid(row=0, column=0, sticky="w", padx=16, pady=(16, 6))
        self.sched_name = ctk.CTkEntry(tab)
        self.sched_name.grid(row=1, column=0, sticky="ew", padx=16)

        ctk.CTkLabel(tab, text="ID станций").grid(row=2, column=0, sticky="w", padx=16, pady=(12, 6))
        self.sched_station_ids = ctk.CTkTextbox(tab, height=80)
        self.sched_station_ids.grid(row=3, column=0, sticky="ew", padx=16)

        opts = ctk.CTkFrame(tab)
        opts.grid(row=4, column=0, sticky="ew", padx=16, pady=(12, 12))
        opts.grid_columnconfigure((0, 1, 2), weight=1)

        ctk.CTkLabel(opts, text="Время (HH:MM)").grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))
        ctk.CTkLabel(opts, text="Режим").grid(row=0, column=1, sticky="w", padx=12, pady=(12, 6))
        ctk.CTkLabel(opts, text="N дней").grid(row=0, column=2, sticky="w", padx=12, pady=(12, 6))

        self.sched_time = ctk.CTkEntry(opts)
        self.sched_time.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
        self.sched_time.insert(0, "09:00")

        self.sched_mode = ctk.CTkOptionMenu(opts, values=["Только вчера", "Последние N дней"])
        self.sched_mode.grid(row=1, column=1, sticky="ew", padx=12, pady=(0, 12))
        self.sched_mode.set("Только вчера")

        self.sched_days_back = ctk.CTkEntry(opts)
        self.sched_days_back.grid(row=1, column=2, sticky="ew", padx=12, pady=(0, 12))
        self.sched_days_back.insert(0, "2")

        ctk.CTkLabel(tab, text="Папка сохранения").grid(row=5, column=0, sticky="w", padx=16, pady=(0, 6))

        sched_out_frame = ctk.CTkFrame(tab)
        sched_out_frame.grid(row=6, column=0, sticky="ew", padx=16, pady=(0, 12))
        sched_out_frame.grid_columnconfigure(0, weight=1)

        self.sched_output_entry = ctk.CTkEntry(sched_out_frame, textvariable=self.sched_output_dir)
        self.sched_output_entry.grid(row=0, column=0, sticky="ew", padx=(12, 8), pady=12)

        ctk.CTkButton(
            sched_out_frame,
            text="Выбрать папку",
            command=self.choose_schedule_output_dir
        ).grid(row=0, column=1, padx=(0, 12), pady=12)

        save_btn = ctk.CTkButton(tab, text="Сохранить задачу", command=self.on_save_schedule)
        save_btn.grid(row=7, column=0, sticky="w", padx=16, pady=(0, 12))

        ctk.CTkLabel(tab, text="Сохранённые задачи", font=ctk.CTkFont(size=18, weight="bold")).grid(
            row=8, column=0, sticky="w", padx=16, pady=(4, 8)
        )

        self.schedule_scroll = ctk.CTkScrollableFrame(tab)
        self.schedule_scroll.grid(row=9, column=0, sticky="nsew", padx=16, pady=(0, 16))
        self.schedule_scroll.grid_columnconfigure(0, weight=1)

    def append_log(self, message: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.insert("end", f"[{stamp}] {message}\n")
        self.log_box.see("end")

    def choose_manual_output_dir(self):
        folder = filedialog.askdirectory(title="Выбери папку для сохранения файлов")
        if folder:
            self.manual_output_dir.set(folder)

    def choose_schedule_output_dir(self):
        folder = filedialog.askdirectory(title="Выбери папку для задачи автопарсинга")
        if folder:
            self.sched_output_dir.set(folder)

    def run_job_in_thread(self, station_ids, start_date, end_date, output_dir, job_name="manual"):
        if self.current_worker_thread and self.current_worker_thread.is_alive():
            self.append_log("[job] Уже идёт другой парсинг. Сначала останови его или дождись завершения.")
            return

        stop_event = threading.Event()
        self.current_stop_event = stop_event
        self.current_job_name = job_name

        def worker():
            try:
                self.append_log(
                    f"[{job_name}] Старт: станции={station_ids or 'все из CSV'}, "
                    f"период={start_date}..{end_date}, папка={output_dir}"
                )
                from parc import run_parse_job
                run_parse_job(
                    station_ids=station_ids,
                    start_date=start_date,
                    end_date=end_date,
                    output_dir=output_dir,
                    log=self.append_log,
                    stop_event=stop_event,
                )

                if stop_event.is_set():
                    self.append_log(f"[{job_name}] Остановлено пользователем.")
                else:
                    self.append_log(f"[{job_name}] Готово.")
            except Exception as e:
                self.append_log(f"[{job_name}] Ошибка: {e}")
            finally:
                self.current_stop_event = None
                self.current_worker_thread = None
                self.current_job_name = None

        self.current_worker_thread = threading.Thread(target=worker, daemon=True)
        self.current_worker_thread.start()
    def stop_current_job(self):
        if self.current_stop_event and self.current_worker_thread and self.current_worker_thread.is_alive():
            self.current_stop_event.set()
            self.append_log(f"[{self.current_job_name or 'job'}] Запрошена остановка. Ждём безопасного завершения...")
        else:
            self.append_log("[job] Сейчас нет активного парсинга.")
    def on_manual_run(self):
        try:
            station_ids = parse_station_ids(self.manual_station_ids.get("1.0", "end").strip())
            start_dt, end_dt = validate_manual_dates(self.manual_start.get().strip(), self.manual_end.get().strip())

            output_dir = self.manual_output_entry.get().strip()
            if not output_dir:
                raise ValueError("Нужно указать папку сохранения")

            Path(output_dir).mkdir(parents=True, exist_ok=True)

            self.run_job_in_thread(
                station_ids=station_ids,
                start_date=start_dt,
                end_date=end_dt,
                output_dir=output_dir,
                job_name="manual",
            )
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))
            self.append_log(f"[manual] Ошибка валидации: {e}")

    def clear_schedule_form(self):
        self.sched_name.delete(0, "end")
        self.sched_station_ids.delete("1.0", "end")
        self.sched_time.delete(0, "end")
        self.sched_time.insert(0, "09:00")
        self.sched_mode.set("Только вчера")
        self.sched_days_back.delete(0, "end")
        self.sched_days_back.insert(0, "2")
        self.sched_output_dir.set(str(DEFAULT_OUTPUT_DIR))
        self.edit_schedule_index = None

    def edit_schedule(self, index: int):
        item = self.scheduler.items[index]
        self.edit_schedule_index = index

        self.sched_name.delete(0, "end")
        self.sched_name.insert(0, item.name)

        self.sched_station_ids.delete("1.0", "end")
        self.sched_station_ids.insert("1.0", item.station_ids_raw)

        self.sched_time.delete(0, "end")
        self.sched_time.insert(0, item.run_time)

        self.sched_mode.set(item.range_mode)

        self.sched_days_back.delete(0, "end")
        self.sched_days_back.insert(0, str(item.days_back))

        self.sched_output_dir.set(item.output_dir)

        self.append_log(f"[scheduler] Задача '{item.name}' загружена для редактирования.")

    def on_save_schedule(self):
        try:
            name = self.sched_name.get().strip()
            if not name:
                raise ValueError("Нужно указать название задачи")

            station_ids_raw = self.sched_station_ids.get("1.0", "end").strip()
            _ = parse_station_ids(station_ids_raw)

            time_value = self.sched_time.get().strip()
            if not re.match(r"^\d{2}:\d{2}$", time_value):
                raise ValueError("Время должно быть в формате HH:MM")

            hh, mm = map(int, time_value.split(":"))
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError("Некорректное время запуска")

            days_back = int(self.sched_days_back.get().strip() or "1")
            if days_back < 1:
                raise ValueError("N дней должно быть не меньше 1")

            output_dir = self.sched_output_entry.get().strip()
            if not output_dir:
                raise ValueError("Нужно указать папку сохранения")
            Path(output_dir).mkdir(parents=True, exist_ok=True)

            item = ScheduleItem(
                name=name,
                station_ids_raw=station_ids_raw,
                run_time=f"{hh:02d}:{mm:02d}",
                range_mode=self.sched_mode.get(),
                days_back=days_back,
                output_dir=output_dir,
                enabled=True,
            )

            if self.edit_schedule_index is None:
                self.scheduler.add(item)
                self.append_log(f"[scheduler] Задача '{name}' сохранена.")
            else:
                old_enabled = self.scheduler.items[self.edit_schedule_index].enabled
                old_last_run_date = self.scheduler.items[self.edit_schedule_index].last_run_date
                item.enabled = old_enabled
                item.last_run_date = old_last_run_date

                self.scheduler.items[self.edit_schedule_index] = item
                self.scheduler.save()
                self.refresh_schedule_list()
                self.append_log(f"[scheduler] Задача '{name}' обновлена.")
                self.edit_schedule_index = None

            self.clear_schedule_form()
            self.refresh_schedule_list()

        except Exception as e:
            messagebox.showerror("Ошибка", str(e))
            self.append_log(f"[scheduler] Ошибка валидации: {e}")

    def refresh_schedule_list(self):
        for widget in self.schedule_scroll.winfo_children():
            widget.destroy()

        if not self.scheduler.items:
            empty = ctk.CTkLabel(self.schedule_scroll, text="Сохранённых задач пока нет.")
            empty.grid(row=0, column=0, sticky="w", padx=8, pady=8)
            return

        for idx, item in enumerate(self.scheduler.items):
            card = ctk.CTkFrame(self.schedule_scroll)
            card.grid(row=idx, column=0, sticky="ew", padx=4, pady=6)
            card.grid_columnconfigure(0, weight=1)

            top = ctk.CTkFrame(card, fg_color="transparent")
            top.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
            top.grid_columnconfigure(0, weight=1)

            title = ctk.CTkLabel(top, text=item.name, font=ctk.CTkFont(size=16, weight="bold"))
            title.grid(row=0, column=0, sticky="w")

            switch = ctk.CTkSwitch(
                top,
                text="Включена",
                command=lambda i=idx: self.scheduler_toggle_from_ui(i),
            )
            switch.grid(row=0, column=1, sticky="e")
            if item.enabled:
                switch.select()
            else:
                switch.deselect()

            text = (
                f"Станции: {item.station_ids_raw or 'все из CSV'}\n"
                f"Время: {item.run_time}\n"
                f"Диапазон: {'только вчера' if item.range_mode == 'Только вчера' else f'последние {item.days_back} дн. до вчера'}\n"
                f"Папка: {item.output_dir}\n"
                f"Последний запуск: {item.last_run_date or 'ещё не было'}"
            )
            body = ctk.CTkLabel(card, text=text, justify="left", anchor="w")
            body.grid(row=1, column=0, sticky="w", padx=10, pady=(0, 8))

            buttons = ctk.CTkFrame(card, fg_color="transparent")
            buttons.grid(row=2, column=0, sticky="w", padx=10, pady=(0, 10))

            ctk.CTkButton(
                buttons,
                text="Изменить",
                width=110,
                command=lambda i=idx: self.edit_schedule(i),
            ).grid(row=0, column=0, padx=(0, 8))

            ctk.CTkButton(
                buttons,
                text="Удалить",
                width=110,
                fg_color="#8B1E1E",
                hover_color="#A52A2A",
                command=lambda i=idx: self.delete_schedule(i),
            ).grid(row=0, column=1)

    def scheduler_toggle_from_ui(self, index: int):
        item = self.scheduler.items[index]
        self.scheduler.toggle(index, not item.enabled)
        current = self.scheduler.items[index]
        self.append_log(
            f"[scheduler] Задача '{current.name}' {'включена' if current.enabled else 'выключена'}."
        )

    def delete_schedule(self, index: int):
        name = self.scheduler.items[index].name
        self.scheduler.delete(index)
        self.append_log(f"[scheduler] Задача '{name}' удалена.")

    def scheduler_tick(self):
        try:
            self.scheduler.poll()
        finally:
            self.after(10_000, self.scheduler_tick)


if __name__ == "__main__":
    app = App()
    app.mainloop()