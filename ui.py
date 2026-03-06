"""
Upbit 자동매매 시스템 — GUI v2
실행: python ui.py
"""
from __future__ import annotations
import os, sys, queue, logging, threading, uuid
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from logging_.system_logger import setup_logging
from core.session_timer import SessionTimer, DURATION_OPTIONS
from core.notification_manager import NotificationManager
from logging_.obsidian_logger import ObsidianLogger


# ─── 로그 큐 핸들러 ───────────────────────────────────────────────────────────

class _QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue) -> None:
        super().__init__()
        self.q = q
    def emit(self, record: logging.LogRecord) -> None:
        try: self.q.put_nowait(self.format(record))
        except queue.Full: pass


# ─── 상수 ─────────────────────────────────────────────────────────────────────

STRATEGIES = {
    "vb_noise_filter  │  변동성돌파 + 노이즈필터 + MA":  ("volatility_breakout", "vb_noise_filter"),
    "vb_standard      │  변동성돌파 표준형 (동적K+타임컷)":  ("volatility_breakout", "vb_standard"),
    "mr_rsi           │  RSI 과매도 평균회귀":           ("mean_reversion",      "mr_rsi"),
    "mr_bollinger     │  볼린저 밴드 하단 매수":         ("mean_reversion",      "mr_bollinger"),
    "scalping_triple_ema    │  삼중EMA 눌림목 (1분봉)": ("scalping",            "scalping_triple_ema"),
    "scalping_bb_rsi        │  BB+RSI 스캘핑 (15분봉)": ("scalping",            "scalping_bb_rsi"),
    "scalping_5ema_reversal │  5EMA 반전 Long (5분봉)":  ("scalping",            "scalping_5ema_reversal"),
    "macd_rsi_trend         │  MACD 골든크로스(제로하) + RSI 추세추종 (1시간봉)": ("trend_following", "macd_rsi_trend"),
    "smrh_stop              │  SMRH 스탑매매 HA돌파 (4h+30m 멀티타임프레임)": ("trend_following", "smrh_stop"),
}
STRATEGY_KEYS = list(STRATEGIES.keys())

ALL_TICKERS: list[str] = []   # 런타임에 동적으로 채워짐

# Catppuccin Mocha 팔레트
C = type("C", (), {
    "BG":      "#1e1e2e",
    "BG2":     "#313244",
    "BG3":     "#45475a",
    "FG":      "#cdd6f4",
    "ACCENT":  "#89b4fa",
    "GREEN":   "#a6e3a1",
    "RED":     "#f38ba8",
    "YELLOW":  "#f9e2af",
    "PEACH":   "#fab387",
    "SUB":     "#6c7086",
    "HEADER":  "#11111b",
})()


# ─── 가상계좌 행 데이터 ───────────────────────────────────────────────────────

class PaperAccountRow:
    """UI에서 가상계좌 하나를 나타내는 데이터 클래스"""
    def __init__(self, account_id: str | None = None) -> None:
        self.account_id           = account_id or f"ACC-{uuid.uuid4().hex[:6].upper()}"
        self.name_var             = tk.StringVar(value=self.account_id)
        self.strategy_var         = tk.StringVar(value=STRATEGY_KEYS[0])
        self.balance_var          = tk.StringVar(value=f"{config.PAPER_DEFAULT_BALANCE:,}")
        self.weight_var           = tk.StringVar(value="")           # 가중치% (자동계산)
        self.ticker_count_var     = tk.StringVar(value=str(config.PAPER_DEFAULT_TICKER_COUNT))
        self.budget_per_trade_var = tk.StringVar(value=f"{config.PAPER_DEFAULT_BUDGET_PER_TRADE:,}")
        self._frame: tk.Frame | None = None


# ─── 메인 앱 ─────────────────────────────────────────────────────────────────

class TradingApp(tk.Tk):

    def __init__(self) -> None:
        super().__init__()
        self.title("Upbit 자동매매 시스템")
        self.geometry("1120x780")
        self.minsize(900, 640)
        self.configure(bg=C.BG)

        # 실행 상태 (실제 / 가상 독립 관리)
        self._trader         = None
        self._paper_engine   = None
        self._session_timer: SessionTimer | None     = None
        self._notif_mgr: NotificationManager | None  = None
        self._obsidian: ObsidianLogger | None        = None
        self._trader_thread: threading.Thread | None = None
        self._real_running  = False
        self._paper_running = False
        self._paper_rows: list[PaperAccountRow] = []
        self._trade_log_wins: dict[str, tk.Toplevel] = {}  # 거래 로그 팝업 창 관리
        self._param_vars: dict[str, dict[str, tk.Variable]] = {}  # 전략 파라미터 슬라이더
        self._git_uploading = False   # GitHub 업로드 진행 여부
        self._reentry_vars: dict[str, tk.BooleanVar] = {}  # 전략별 재진입 토글

        # 로그 큐
        self._log_queue: queue.Queue = queue.Queue(maxsize=2000)

        self._setup_logging()
        self._apply_style()
        self._build_ui()
        self._poll_logs()
        self._poll_status()
        self.after(500, self._refresh_tickers)   # 시작 시 거래량 상위 종목 자동 로드
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ─── 로깅 ────────────────────────────────────────────────────────────────

    def _setup_logging(self) -> None:
        setup_logging()
        h = _QueueHandler(self._log_queue)
        h.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)-8s] %(name)-20s | %(message)s",
            datefmt="%H:%M:%S",
        ))
        h.setLevel(logging.INFO)
        logging.getLogger().addHandler(h)

    # ─── TTK 스타일 ──────────────────────────────────────────────────────────

    def _apply_style(self) -> None:
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("TNotebook",       background=C.BG,  borderwidth=0)
        s.configure("TNotebook.Tab",   background=C.BG2, foreground=C.FG,
                    padding=[12, 5], font=("Arial", 9))
        s.map("TNotebook.Tab",
              background=[("selected", C.BG3)],
              foreground=[("selected", C.ACCENT)])
        s.configure("Treeview",        background=C.BG2, foreground=C.FG,
                    fieldbackground=C.BG2, rowheight=26, font=("Consolas", 9))
        s.configure("Treeview.Heading", background=C.BG3, foreground=C.ACCENT,
                    font=("Arial", 9, "bold"))
        s.map("Treeview", background=[("selected", C.BG3)])
        s.configure("TCombobox",       fieldbackground=C.BG3,
                    background=C.BG3, foreground=C.FG, arrowcolor=C.FG)
        s.map("TCombobox", fieldbackground=[("readonly", C.BG3)])
        s.configure("Vertical.TScrollbar", background=C.BG3,
                    troughcolor=C.BG2, arrowcolor=C.FG)

    # ─── UI 전체 구조 ─────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._build_topbar()

        main = tk.Frame(self, bg=C.BG)
        main.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        left = tk.Frame(main, bg=C.BG2, width=380)
        left.pack(side="left", fill="y", padx=(0, 6))
        left.pack_propagate(False)
        self._build_left_panel(left)

        right = tk.Frame(main, bg=C.BG)
        right.pack(side="left", fill="both", expand=True)
        self._build_right_panel(right)

    # ─── 상단바 ──────────────────────────────────────────────────────────────

    def _build_topbar(self) -> None:
        bar = tk.Frame(self, bg=C.HEADER, pady=9)
        bar.pack(fill="x")

        # 실제거래 상태 인디케이터
        self._real_dot = tk.Label(bar, text="●", font=("Arial", 13),
                                   fg=C.RED, bg=C.HEADER)
        self._real_dot.pack(side="left", padx=(12, 2))
        self._real_lbl = tk.Label(bar, text="실제: 정지",
                                   font=("Arial", 10, "bold"),
                                   fg=C.FG, bg=C.HEADER)
        self._real_lbl.pack(side="left")

        tk.Label(bar, text="  │  ", font=("Arial", 10),
                 fg=C.SUB, bg=C.HEADER).pack(side="left")

        # 가상거래 상태 인디케이터
        self._paper_dot = tk.Label(bar, text="●", font=("Arial", 13),
                                    fg=C.RED, bg=C.HEADER)
        self._paper_dot.pack(side="left", padx=(0, 2))
        self._paper_lbl = tk.Label(bar, text="가상: 정지",
                                    font=("Arial", 10, "bold"),
                                    fg=C.FG, bg=C.HEADER)
        self._paper_lbl.pack(side="left")

        tk.Label(bar, text="  Upbit 자동매매 시스템",
                 font=("Arial", 12, "bold"), fg=C.ACCENT,
                 bg=C.HEADER).pack(side="left", padx=10)

        # 우측: 타이머 / 자산 / GitHub 업로드
        self._timer_label = tk.Label(bar, text="⏱ —",
                                      font=("Consolas", 10), fg=C.YELLOW,
                                      bg=C.HEADER)
        self._timer_label.pack(side="right", padx=8)
        self._equity_label = tk.Label(bar, text="자산: —",
                                       font=("Arial", 11, "bold"),
                                       fg=C.GREEN, bg=C.HEADER)
        self._equity_label.pack(side="right", padx=12)

        # GitHub 자동 업로드 버튼
        tk.Label(bar, text="│", fg=C.SUB, bg=C.HEADER).pack(side="right", padx=4)
        self._git_btn = tk.Button(
            bar, text="↑ GitHub",
            font=("Arial", 9, "bold"),
            bg=C.BG2, fg=C.ACCENT,
            relief="flat", bd=0, padx=10, pady=3,
            cursor="hand2",
            command=self._on_git_upload,
        )
        self._git_btn.pack(side="right", padx=(4, 0))
        self._git_status_label = tk.Label(
            bar, text="",
            font=("Arial", 8), fg=C.SUB, bg=C.HEADER,
        )
        self._git_status_label.pack(side="right", padx=(0, 2))

    # ─── 좌측 패널 ───────────────────────────────────────────────────────────

    def _build_left_panel(self, parent: tk.Frame) -> None:
        nb = ttk.Notebook(parent)
        nb.pack(fill="both", expand=True)

        settings_frame = tk.Frame(nb, bg=C.BG2)
        nb.add(settings_frame, text="  거래 설정  ")
        self._build_trade_settings(settings_frame)

        self._paper_tab_frame = tk.Frame(nb, bg=C.BG2)
        nb.add(self._paper_tab_frame, text="  가상계좌  ")
        self._build_paper_tab(self._paper_tab_frame)

        params_frame = tk.Frame(nb, bg=C.BG2)
        nb.add(params_frame, text="  파라미터  ")
        self._build_params_tab(params_frame)

        notif_frame = tk.Frame(nb, bg=C.BG2)
        nb.add(notif_frame, text="  알림/기록  ")
        self._build_notif_tab(notif_frame)

        self._left_notebook = nb

    def _build_trade_settings(self, parent: tk.Frame) -> None:
        def lbl(f, txt): return tk.Label(f, text=txt, font=("Arial", 9), fg=C.FG,
                                          bg=C.BG2, width=9, anchor="w")

        # ════════════════════════════════════════════════════════════════════
        # ① 하단 고정 영역을 side="bottom"으로 먼저 pack (역순 → 화면상 정순)
        # ════════════════════════════════════════════════════════════════════

        # 가상거래 버튼 행 (맨 아래)
        bf2 = tk.Frame(parent, bg=C.BG2)
        bf2.pack(side="bottom", fill="x", padx=12, pady=(2, 12))
        self._paper_start_btn = tk.Button(
            bf2, text="▶  가상 시작", font=("Arial", 10, "bold"),
            bg=C.ACCENT, fg=C.HEADER, relief="flat", bd=0, pady=7,
            cursor="hand2", command=self._on_start_paper_btn,
        )
        self._paper_start_btn.pack(side="left", fill="x", expand=True, padx=(0, 3))
        self._paper_stop_btn = tk.Button(
            bf2, text="■  정지", font=("Arial", 10, "bold"),
            bg=C.BG3, fg=C.FG, relief="flat", bd=0, pady=7,
            state="disabled", cursor="hand2", command=self._on_stop_paper_btn,
        )
        self._paper_stop_btn.pack(side="left", fill="x", expand=True, padx=(3, 0))

        tk.Label(parent, text="가상거래", font=("Arial", 8, "bold"),
                 fg=C.ACCENT, bg=C.BG2).pack(side="bottom", anchor="w", padx=14)

        # 실제거래 버튼 행
        bf1 = tk.Frame(parent, bg=C.BG2)
        bf1.pack(side="bottom", fill="x", padx=12, pady=(2, 5))
        self._real_start_btn = tk.Button(
            bf1, text="▶  실제 시작", font=("Arial", 10, "bold"),
            bg=C.PEACH, fg=C.HEADER, relief="flat", bd=0, pady=7,
            cursor="hand2", command=self._on_start_real,
        )
        self._real_start_btn.pack(side="left", fill="x", expand=True, padx=(0, 3))
        self._real_stop_btn = tk.Button(
            bf1, text="■  정지", font=("Arial", 10, "bold"),
            bg=C.BG3, fg=C.FG, relief="flat", bd=0, pady=7,
            state="disabled", cursor="hand2", command=self._on_stop_real,
        )
        self._real_stop_btn.pack(side="left", fill="x", expand=True, padx=(3, 0))

        tk.Label(parent, text="실제거래", font=("Arial", 8, "bold"),
                 fg=C.PEACH, bg=C.BG2).pack(side="bottom", anchor="w", padx=14)

        tk.Frame(parent, bg=C.BG3, height=1).pack(
            side="bottom", fill="x", padx=10, pady=(8, 0))

        # 세션 시간
        self._session_var = tk.StringVar(value="무제한")
        ttk.Combobox(parent, textvariable=self._session_var,
                     values=list(DURATION_OPTIONS.keys()), state="readonly",
                     font=("Arial", 9)).pack(side="bottom", fill="x", padx=12, pady=(0, 4))
        tk.Label(parent, text="▶  세션 시간", font=("Arial", 9, "bold"),
                 fg=C.ACCENT, bg=C.BG2).pack(side="bottom", anchor="w", padx=12, pady=(4, 0))
        tk.Frame(parent, bg=C.BG3, height=1).pack(
            side="bottom", fill="x", padx=10, pady=(10, 0))

        # 낙폭
        self._drawdown_var = tk.IntVar(value=int(config.MAX_DRAWDOWN_PCT * 100))
        f3 = tk.Frame(parent, bg=C.BG2)
        f3.pack(side="bottom", fill="x", padx=12, pady=3)
        lbl(f3, "낙폭(%)").pack(side="left")
        tk.Scale(f3, variable=self._drawdown_var, from_=5, to=50,
                 orient="horizontal", bg=C.BG2, fg=C.FG,
                 troughcolor=C.BG3, highlightthickness=0,
                 showvalue=False).pack(side="left", fill="x", expand=True)
        tk.Label(f3, textvariable=self._drawdown_var, width=3,
                 font=("Arial", 9, "bold"), fg=C.YELLOW, bg=C.BG2).pack(side="left")

        # 손절
        self._stoploss_var = tk.IntVar(value=int(config.STOP_LOSS_PCT * 100))
        f2 = tk.Frame(parent, bg=C.BG2)
        f2.pack(side="bottom", fill="x", padx=12, pady=3)
        lbl(f2, "손절(%)").pack(side="left")
        tk.Scale(f2, variable=self._stoploss_var, from_=1, to=20,
                 orient="horizontal", bg=C.BG2, fg=C.FG,
                 troughcolor=C.BG3, highlightthickness=0,
                 showvalue=False).pack(side="left", fill="x", expand=True)
        tk.Label(f2, textvariable=self._stoploss_var, width=3,
                 font=("Arial", 9, "bold"), fg=C.RED, bg=C.BG2).pack(side="left")

        # 예산
        f1 = tk.Frame(parent, bg=C.BG2)
        f1.pack(side="bottom", fill="x", padx=12, pady=3)
        lbl(f1, "예산(원)").pack(side="left")
        self._budget_var = tk.StringVar(value=str(config.BUDGET_PER_TRADE))
        tk.Entry(f1, textvariable=self._budget_var, font=("Consolas", 9),
                 bg=C.BG3, fg=C.FG, insertbackground=C.FG,
                 relief="flat", bd=4).pack(side="left", fill="x", expand=True)

        tk.Label(parent, text="▶  거래 설정", font=("Arial", 9, "bold"),
                 fg=C.ACCENT, bg=C.BG2).pack(side="bottom", anchor="w", padx=12, pady=(4, 2))
        tk.Frame(parent, bg=C.BG3, height=1).pack(
            side="bottom", fill="x", padx=10, pady=(10, 0))

        # ════════════════════════════════════════════════════════════════════
        # ② 상단 고정 영역: 전략 + 종목 헤더 (side="top", 기본값)
        # ════════════════════════════════════════════════════════════════════

        # 전략 선택
        tk.Frame(parent, bg=C.BG3, height=1).pack(fill="x", padx=10, pady=(10, 0))
        tk.Label(parent, text="▶  전략 선택", font=("Arial", 9, "bold"),
                 fg=C.ACCENT, bg=C.BG2).pack(anchor="w", padx=12, pady=(4, 2))
        self._strategy_var = tk.StringVar(value=STRATEGY_KEYS[0])
        ttk.Combobox(parent, textvariable=self._strategy_var,
                     values=STRATEGY_KEYS, state="readonly",
                     font=("Consolas", 8)).pack(fill="x", padx=12, pady=4)

        # 종목 헤더
        self._ticker_vars: dict[str, tk.BooleanVar] = {}
        tk.Frame(parent, bg=C.BG3, height=1).pack(fill="x", padx=10, pady=(10, 0))

        th = tk.Frame(parent, bg=C.BG2)
        th.pack(fill="x", padx=12, pady=(4, 0))
        tk.Label(th, text="▶  거래 종목", font=("Arial", 9, "bold"),
                 fg=C.ACCENT, bg=C.BG2).pack(side="left")
        self._ticker_status = tk.Label(th, text="로딩 중...",
                                        font=("Arial", 8), fg=C.YELLOW, bg=C.BG2)
        self._ticker_status.pack(side="left", padx=(6, 0))
        self._ticker_sel_label = tk.Label(th, text="", font=("Arial", 8),
                                           fg=C.GREEN, bg=C.BG2)
        self._ticker_sel_label.pack(side="left", padx=(4, 0))
        tk.Button(th, text="↻ 새로고침", font=("Arial", 8),
                  bg=C.BG3, fg=C.FG, relief="flat", bd=2, padx=6,
                  cursor="hand2", command=self._refresh_tickers
                  ).pack(side="right")

        # 일괄 선택/해제 버튼
        tb = tk.Frame(parent, bg=C.BG2)
        tb.pack(fill="x", padx=12, pady=(2, 2))
        tk.Button(tb, text="✔ 전체선택", font=("Arial", 8),
                  bg=C.BG3, fg=C.GREEN, relief="flat", bd=2, padx=8,
                  cursor="hand2", command=self._select_all_tickers
                  ).pack(side="left", padx=(0, 4))
        tk.Button(tb, text="✘ 전체해제", font=("Arial", 8),
                  bg=C.BG3, fg=C.YELLOW, relief="flat", bd=2, padx=8,
                  cursor="hand2", command=self._deselect_all_tickers
                  ).pack(side="left")

        # ════════════════════════════════════════════════════════════════════
        # ③ 종목 캔버스: 남은 공간 전부 차지 (fill="both", expand=True)
        # ════════════════════════════════════════════════════════════════════
        wrap = tk.Frame(parent, bg=C.BG3, bd=1, relief="flat")
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 4))

        self._ticker_canvas = tk.Canvas(wrap, bg=C.BG2, highlightthickness=0)
        t_scroll = ttk.Scrollbar(wrap, orient="vertical",
                                  command=self._ticker_canvas.yview)
        self._ticker_inner = tk.Frame(self._ticker_canvas, bg=C.BG2)
        self._ticker_inner.bind(
            "<Configure>",
            lambda e: self._ticker_canvas.configure(
                scrollregion=self._ticker_canvas.bbox("all")
            ),
        )
        self._ticker_canvas.create_window((0, 0), window=self._ticker_inner, anchor="nw")
        self._ticker_canvas.configure(yscrollcommand=t_scroll.set)
        self._ticker_canvas.pack(side="left", fill="both", expand=True)
        t_scroll.pack(side="right", fill="y")

        # 캔버스·내부 프레임 양쪽에 마우스휠 바인딩
        _scroll_ticker = lambda e: self._ticker_canvas.yview_scroll(
            -1 * (e.delta // 120), "units"
        )
        self._ticker_canvas.bind("<MouseWheel>", _scroll_ticker)
        self._ticker_inner.bind("<MouseWheel>", _scroll_ticker)
        # 체크박스 마우스휠 전달용 (populate 시 각 위젯에도 바인딩)
        self._ticker_scroll_fn = _scroll_ticker

    def _build_paper_tab(self, parent: tk.Frame) -> None:
        # ── 상단: 전체 예산 입력 + 균등 배분 ──
        hdr = tk.Frame(parent, bg=C.BG2)
        hdr.pack(fill="x", padx=8, pady=(8, 2))
        tk.Label(hdr, text="전체 예산(원):", font=("Arial", 9, "bold"),
                 fg=C.ACCENT, bg=C.BG2).pack(side="left")
        self._paper_total_budget_var = tk.StringVar(
            value=f"{config.PAPER_TOTAL_BUDGET:,}")
        tk.Entry(hdr, textvariable=self._paper_total_budget_var, width=12,
                 font=("Consolas", 9), bg=C.BG3, fg=C.FG,
                 insertbackground=C.FG, relief="flat", bd=3).pack(side="left", padx=4)
        tk.Button(hdr, text="균등 배분", font=("Arial", 8, "bold"),
                  bg=C.ACCENT, fg=C.HEADER, relief="flat", bd=0, padx=6, pady=2,
                  cursor="hand2", command=self._distribute_paper_budget_equally
                  ).pack(side="left", padx=4)

        self._paper_budget_info_label = tk.Label(
            parent, text="", font=("Arial", 8), fg=C.SUB, bg=C.BG2)
        self._paper_budget_info_label.pack(anchor="w", padx=12, pady=(0, 2))

        # ── 중간: 스크롤 가능 카드 영역 ──
        wrap = tk.Frame(parent, bg=C.BG3, bd=1, relief="flat")
        wrap.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        self._paper_canvas = tk.Canvas(wrap, bg=C.BG2, highlightthickness=0)
        p_scroll = ttk.Scrollbar(wrap, orient="vertical",
                                  command=self._paper_canvas.yview)
        self._paper_inner = tk.Frame(self._paper_canvas, bg=C.BG2)
        self._paper_inner.bind(
            "<Configure>",
            lambda e: self._paper_canvas.configure(
                scrollregion=self._paper_canvas.bbox("all")
            ),
        )
        self._paper_canvas.create_window((0, 0), window=self._paper_inner, anchor="nw")
        self._paper_canvas.configure(yscrollcommand=p_scroll.set)
        self._paper_canvas.pack(side="left", fill="both", expand=True)
        p_scroll.pack(side="right", fill="y")

        _scroll_paper = lambda e: self._paper_canvas.yview_scroll(
            -1 * (e.delta // 120), "units"
        )
        self._paper_canvas.bind("<MouseWheel>", _scroll_paper)
        self._paper_inner.bind("<MouseWheel>", _scroll_paper)
        self._paper_scroll_fn = _scroll_paper

        # 전략 개수만큼 가상계좌 자동 생성 (각 전략 1개씩)
        for i, key in enumerate(STRATEGY_KEYS):
            row = PaperAccountRow()
            row.name_var.set(f"ACC-{i+1:02d}")
            row.strategy_var.set(key)
            self._paper_rows.append(row)
            self._render_paper_row(row)

        # ── 하단: 추가/삭제 버튼 ──
        bf = tk.Frame(parent, bg=C.BG2)
        bf.pack(fill="x", padx=8, pady=(0, 6))
        tk.Button(bf, text="+ 계좌 추가", font=("Arial", 9),
                  bg=C.ACCENT, fg=C.HEADER, relief="flat", bd=0, padx=8, pady=4,
                  cursor="hand2", command=self._add_paper_row
                  ).pack(side="left", padx=(0, 4))
        tk.Button(bf, text="− 마지막 삭제", font=("Arial", 9),
                  bg=C.BG3, fg=C.FG, relief="flat", bd=0, padx=8, pady=4,
                  cursor="hand2", command=self._remove_paper_row
                  ).pack(side="left")

        # 초기 균등 배분
        self._distribute_paper_budget_equally()

    def _render_paper_row(self, row: PaperAccountRow) -> None:
        """카드형 2줄 레이아웃으로 시나리오 표시"""
        card = tk.Frame(self._paper_inner, bg=C.BG3, bd=1, relief="flat")
        card.pack(fill="x", padx=4, pady=3, ipady=2)
        row._frame = card

        # 마우스휠 전달
        if hasattr(self, "_paper_scroll_fn"):
            card.bind("<MouseWheel>", self._paper_scroll_fn)

        # ── 1줄: 계좌명 + 전략 ──
        line1 = tk.Frame(card, bg=C.BG3)
        line1.pack(fill="x", padx=6, pady=(3, 1))
        tk.Label(line1, textvariable=row.name_var, font=("Consolas", 9, "bold"),
                 fg=C.ACCENT, bg=C.BG3, width=8, anchor="w").pack(side="left")
        ttk.Combobox(line1, textvariable=row.strategy_var,
                     values=STRATEGY_KEYS, state="readonly",
                     font=("Consolas", 7), width=22).pack(side="left", padx=4)

        # ── 2줄: 자금 + 가중치 + 종목수 + 거래예산 ──
        line2 = tk.Frame(card, bg=C.BG3)
        line2.pack(fill="x", padx=6, pady=(1, 3))

        tk.Label(line2, textvariable=row.balance_var, font=("Consolas", 8),
                 fg=C.GREEN, bg=C.BG3, width=9, anchor="e").pack(side="left")
        tk.Label(line2, textvariable=row.weight_var, font=("Arial", 8),
                 fg=C.YELLOW, bg=C.BG3, width=7).pack(side="left", padx=(2, 4))

        tk.Label(line2, text="종목", font=("Arial", 8), fg=C.SUB,
                 bg=C.BG3).pack(side="left")
        ttk.Combobox(line2, textvariable=row.ticker_count_var,
                     values=[str(x) for x in config.PAPER_TICKER_COUNT_OPTIONS],
                     state="readonly", font=("Consolas", 8), width=4
                     ).pack(side="left", padx=2)

        tk.Label(line2, text="1회", font=("Arial", 8), fg=C.SUB,
                 bg=C.BG3).pack(side="left", padx=(4, 0))
        tk.Entry(line2, textvariable=row.budget_per_trade_var, width=7,
                 font=("Consolas", 8), bg=C.BG2, fg=C.FG,
                 insertbackground=C.FG, relief="flat", bd=2).pack(side="left", padx=2)

    def _distribute_paper_budget_equally(self) -> None:
        """전체 예산을 시나리오별 균등 배분"""
        try:
            total = int(self._paper_total_budget_var.get().replace(",", ""))
        except (ValueError, AttributeError):
            total = config.PAPER_TOTAL_BUDGET
        n = len(self._paper_rows)
        if n == 0:
            return
        per = total // n
        wt = 100.0 / n
        for row in self._paper_rows:
            row.weight_var.set(f"({wt:.1f}%)")
            row.balance_var.set(f"{per:,}")
        if hasattr(self, "_paper_budget_info_label"):
            self._paper_budget_info_label.config(
                text=f"{n}개 시나리오 | 각 {per:,}원 ({wt:.1f}%)")

    def _add_paper_row(self) -> None:
        row = PaperAccountRow()
        row.name_var.set(f"ACC-{len(self._paper_rows)+1:02d}")
        self._paper_rows.append(row)
        self._render_paper_row(row)
        self._distribute_paper_budget_equally()

    def _remove_paper_row(self) -> None:
        if len(self._paper_rows) > 1:
            row = self._paper_rows.pop()
            if row._frame:
                row._frame.destroy()
            self._distribute_paper_budget_equally()

    def _build_params_tab(self, parent: tk.Frame) -> None:
        """전략별 핵심 파라미터 슬라이더 탭."""
        tk.Label(parent, text="거래 시작 전에 적용됩니다  (슬라이더 조정 후 시작)",
                 font=("Arial", 8), fg=C.SUB, bg=C.BG2).pack(anchor="w", padx=10, pady=(6, 2))

        # ── 스크롤 가능 영역 ──
        wrap = tk.Frame(parent, bg=C.BG2)
        wrap.pack(fill="both", expand=True, padx=6, pady=(0, 4))
        canvas = tk.Canvas(wrap, bg=C.BG2, highlightthickness=0)
        sc = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=C.BG2)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sc.set)
        canvas.pack(side="left", fill="both", expand=True)
        sc.pack(side="right", fill="y")
        canvas.bind("<MouseWheel>",
                    lambda e: canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        # ── 헬퍼: 섹션 헤더 ──
        def section(txt: str) -> None:
            tk.Frame(inner, bg=C.BG3, height=1).pack(fill="x", padx=4, pady=(10, 0))
            tk.Label(inner, text=txt, font=("Arial", 9, "bold"),
                     fg=C.ACCENT, bg=C.BG2).pack(anchor="w", padx=8, pady=(3, 1))

        # ── 헬퍼: 슬라이더 행 ──
        def slider_row(label: str, var: tk.Variable,
                       from_: float, to_: float, resolution: float,
                       fmt: str = "{:.0f}") -> None:
            f = tk.Frame(inner, bg=C.BG2)
            f.pack(fill="x", padx=8, pady=1)
            tk.Label(f, text=label, font=("Arial", 8), fg=C.FG,
                     bg=C.BG2, width=16, anchor="w").pack(side="left")
            tk.Scale(
                f, variable=var, from_=from_, to=to_, resolution=resolution,
                orient="horizontal", bg=C.BG2, fg=C.FG,
                troughcolor=C.BG3, highlightthickness=0, showvalue=False, length=100,
            ).pack(side="left", fill="x", expand=True)
            val_lbl = tk.Label(f, text="", font=("Consolas", 9, "bold"),
                               fg=C.YELLOW, bg=C.BG2, width=7, anchor="e")
            val_lbl.pack(side="left")

            def _upd(*_):
                val_lbl.config(text=fmt.format(var.get()))
            var.trace_add("write", _upd)
            _upd()

        p = config.STRATEGY_PARAMS

        # ── 헬퍼: 재진입 토글 ──
        def reentry_row(scenario_id: str) -> None:
            """전략별 수익 재진입 토글 체크박스."""
            var = tk.BooleanVar(value=(scenario_id in config.REENTRY_ENABLED_SCENARIOS))
            self._reentry_vars[scenario_id] = var

            def _on_toggle(*_):
                if var.get():
                    config.REENTRY_ENABLED_SCENARIOS.add(scenario_id)
                else:
                    config.REENTRY_ENABLED_SCENARIOS.discard(scenario_id)

            var.trace_add("write", _on_toggle)
            rf = tk.Frame(inner, bg=C.BG2); rf.pack(fill="x", padx=8, pady=(2, 4))
            tk.Checkbutton(
                rf,
                text="🔄  수익 구간 매도 신호 → 재진입 (매도 없이 단가 갱신)",
                variable=var,
                font=("Arial", 8), fg=C.PEACH, bg=C.BG2,
                selectcolor=C.BG3, activebackground=C.BG2, activeforeground=C.PEACH,
                cursor="hand2",
            ).pack(side="left")

        # ── VB 변동성돌파 (공통) ──
        section("▶  VB 변동성돌파 (공통)")
        vb = {
            "noise_filter_days": tk.IntVar(value=p["vb"]["noise_filter_days"]),
            "ma_period":         tk.IntVar(value=p["vb"]["ma_period"]),
            "k_min":             tk.DoubleVar(value=p["vb"]["k_min"]),
            "k_max":             tk.DoubleVar(value=p["vb"]["k_max"]),
            "time_cut_hours":    tk.DoubleVar(value=p["vb"]["time_cut_hours"]),
            "min_momentum_pct":  tk.DoubleVar(value=p["vb"]["min_momentum_pct"]),
            "vol_mult":          tk.DoubleVar(value=p["vb"]["vol_mult"]),
        }
        slider_row("노이즈 기간 (일)",      vb["noise_filter_days"], 3,   14,  1,    "{:.0f}일")
        slider_row("MA 기간",               vb["ma_period"],          5,   50,  1,    "{:.0f}")
        slider_row("K 클램프 하한",         vb["k_min"],              0.1, 0.5, 0.05, "{:.2f}")
        slider_row("K 클램프 상한",         vb["k_max"],              0.5, 1.0, 0.05, "{:.2f}")
        slider_row("타임컷 시간 (0=비활)",   vb["time_cut_hours"],    0,   12,  0.5,  "{:.1f}h")
        slider_row("최소 수익률 기준 (%)",   vb["min_momentum_pct"],  0.0, 3.0, 0.1,  "{:.1f}%")
        slider_row("거래량 급증 배수 (5분봉)", vb["vol_mult"],         1.0, 5.0, 0.5,  "×{:.1f}")
        self._param_vars["vb"] = vb
        reentry_row("vb_noise_filter")
        reentry_row("vb_standard")

        # ── mr_rsi ──
        section("▶  RSI 과매도 (mr_rsi)")
        rsi_p = p["mr_rsi"]
        rsi = {
            "rsi_buy":       tk.DoubleVar(value=rsi_p["rsi_buy"]),
            "rsi_buy_range": tk.DoubleVar(value=rsi_p["rsi_buy_range"]),
            "rsi_sell":      tk.DoubleVar(value=rsi_p["rsi_sell"]),
            "adx_range_thr": tk.DoubleVar(value=rsi_p["adx_range_thr"]),
            "max_hold_hours":tk.DoubleVar(value=rsi_p["max_hold_hours"]),
        }
        slider_row("RSI 매수 기준 (<)",      rsi["rsi_buy"],       20, 50, 1,   "RSI<{:.0f}")
        slider_row("RSI 완화 매수 (<,약횡보)", rsi["rsi_buy_range"], 25, 55, 1,   "RSI<{:.0f}")
        slider_row("RSI 매도 기준 (>)",      rsi["rsi_sell"],      50, 80, 1,   "RSI>{:.0f}")
        slider_row("ADX 횡보 분기 기준",     rsi["adx_range_thr"], 10, 30, 1,   "ADX<{:.0f}")
        slider_row("최대 보유 시간",         rsi["max_hold_hours"], 6, 72, 2,   "{:.0f}h")
        self._param_vars["mr_rsi"] = rsi
        reentry_row("mr_rsi")

        # ── mr_bollinger ──
        section("▶  볼린저+RSI (mr_bollinger)")
        bb_p = p["mr_bollinger"]
        bb = {
            "rsi_buy":       tk.DoubleVar(value=bb_p["rsi_buy"]),
            "adx_limit":     tk.DoubleVar(value=bb_p["adx_limit"]),
            "bb_period":     tk.IntVar(value=bb_p["bb_period"]),
            "bb_std_trend":  tk.DoubleVar(value=bb_p["bb_std_trend"]),
            "bb_std_range":  tk.DoubleVar(value=bb_p["bb_std_range"]),
            "adx_range_thr": tk.DoubleVar(value=bb_p["adx_range_thr"]),
            "max_hold_hours":tk.DoubleVar(value=bb_p["max_hold_hours"]),
        }
        slider_row("RSI 매수 기준 (<)",     bb["rsi_buy"],       20,  50,  1,    "RSI<{:.0f}")
        slider_row("ADX 횡보 한도 (<)",     bb["adx_limit"],     15,  40,  1,    "ADX<{:.0f}")
        slider_row("BB 기간",               bb["bb_period"],     10,  40,  1,    "{:.0f}")
        slider_row("BB std (추세장)",        bb["bb_std_trend"],  1.0, 3.0, 0.1, "σ{:.1f}")
        slider_row("BB std (약횡보)",        bb["bb_std_range"],  0.8, 2.5, 0.1, "σ{:.1f}")
        slider_row("ADX 횡보 분기 기준",    bb["adx_range_thr"], 10,  30,  1,    "ADX<{:.0f}")
        slider_row("최대 보유 시간",        bb["max_hold_hours"], 12, 120, 6,    "{:.0f}h")
        self._param_vars["mr_bollinger"] = bb
        reentry_row("mr_bollinger")

        # ── scalping_triple_ema ──
        section("▶  삼중EMA 스캘핑 (triple_ema)")
        ema_p = p["scalping_triple_ema"]
        ema = {
            "tp_pct":         tk.DoubleVar(value=ema_p["tp_pct"]),
            "sl_pct":         tk.DoubleVar(value=ema_p["sl_pct"]),
            "adx_min":        tk.DoubleVar(value=ema_p["adx_min"]),
            "ema_spread_min": tk.DoubleVar(value=ema_p["ema_spread_min"]),
            "trail_min_pct":  tk.DoubleVar(value=ema_p["trail_min_pct"]),
        }
        slider_row("Trailing 활성 TP (%)", ema["tp_pct"],         0.2,  3.0,  0.1,  "{:.1f}%")
        slider_row("손절/Trailing 폭 (%)", ema["sl_pct"],         0.1,  1.5,  0.05, "{:.2f}%")
        slider_row("ADX 최소 (횡보필터)",   ema["adx_min"],        10,   35,   1,    "ADX≥{:.0f}")
        slider_row("EMA 이격도 최소 (%)",   ema["ema_spread_min"], 0.0,  1.5,  0.1,  "≥{:.1f}%")
        slider_row("트레일링 최소 폭 (%)",  ema["trail_min_pct"],  0.5,  3.0,  0.1,  "≥{:.1f}%")
        self._param_vars["scalping_triple_ema"] = ema

        # ── scalping_bb_rsi ──
        section("▶  BB+RSI 스캘핑 (bb_rsi)")
        sbb_p = p["scalping_bb_rsi"]
        sbb = {
            "rsi_buy":   tk.DoubleVar(value=sbb_p["rsi_buy"]),
            "adx_limit": tk.DoubleVar(value=sbb_p["adx_limit"]),
            "atr_mult":  tk.DoubleVar(value=sbb_p["atr_mult"]),
        }
        slider_row("RSI 매수 기준 (<)", sbb["rsi_buy"],   20, 50,  1,   "RSI<{:.0f}")
        slider_row("ADX 횡보 한도 (<)", sbb["adx_limit"], 15, 40,  1,   "ADX<{:.0f}")
        slider_row("ATR 손절 배수",      sbb["atr_mult"],  0.5, 3.0, 0.1, "×{:.1f}")
        self._param_vars["scalping_bb_rsi"] = sbb
        reentry_row("scalping_bb_rsi")

        # ── scalping_5ema_reversal ──
        section("▶  5EMA 반전 스캘핑 (5ema)")
        e5_p = p["scalping_5ema_reversal"]
        e5 = {
            "rr_ratio":          tk.DoubleVar(value=e5_p["rr_ratio"]),
            "adx_min":           tk.DoubleVar(value=e5_p["adx_min"]),
            "rsi_entry_max":     tk.DoubleVar(value=e5_p["rsi_entry_max"]),
            "vol_mult":          tk.DoubleVar(value=e5_p["vol_mult"]),
            "time_cut_min":      tk.DoubleVar(value=e5_p["time_cut_min"]),
            "min_momentum_pct":  tk.DoubleVar(value=e5_p["min_momentum_pct"]),
        }
        slider_row("손익비 RR (1:X)",     e5["rr_ratio"],         1.5, 6.0,  0.5, "1:{:.1f}")
        slider_row("ADX 최소 (횡보필터)", e5["adx_min"],          10,  35,   1,   "ADX≥{:.0f}")
        slider_row("RSI 최대 진입 (<)",   e5["rsi_entry_max"],    25,  55,   1,   "RSI<{:.0f}")
        slider_row("거래량 급증 배수",     e5["vol_mult"],          1.0, 3.0,  0.1, "×{:.1f}")
        slider_row("타임컷 시간 (분)",     e5["time_cut_min"],      5,   60,   5,   "{:.0f}분")
        slider_row("최소 수익률 기준 (%)", e5["min_momentum_pct"], 0.0, 2.0,  0.1, "{:.1f}%")
        self._param_vars["scalping_5ema_reversal"] = e5
        reentry_row("scalping_5ema_reversal")

        # ── macd_rsi_trend ──
        section("▶  MACD+RSI 추세추종 (macd_rsi_trend)")
        mrt_p = p["macd_rsi_trend"]
        mrt = {
            "rsi_entry_min": tk.DoubleVar(value=mrt_p["rsi_entry_min"]),
            "rsi_sl":        tk.DoubleVar(value=mrt_p["rsi_sl"]),
            "vol_mult":      tk.DoubleVar(value=mrt_p["vol_mult"]),
        }
        slider_row("RSI 최소 진입 기준", mrt["rsi_entry_min"], 50.0, 60.0, 0.5, "RSI>{:.1f}")
        slider_row("RSI 손절 기준 (<)",  mrt["rsi_sl"],        35.0, 50.0, 0.5, "RSI<{:.1f}")
        slider_row("거래량 급증 배수",    mrt["vol_mult"],       1.0,  3.0,  0.1, "×{:.1f}")
        self._param_vars["macd_rsi_trend"] = mrt
        reentry_row("macd_rsi_trend")

        # ── smrh_stop ──
        section("▶  SMRH 스탑매매 (smrh_stop)")
        smrh_p = p["smrh_stop"]
        smrh = {
            "rsi_min":     tk.DoubleVar(value=smrh_p["rsi_min"]),
            "macd_signal": tk.DoubleVar(value=smrh_p["macd_signal"]),
        }
        slider_row("RSI 최소 기준 (4h+30m)", smrh["rsi_min"],     45.0, 60.0,  0.5, "RSI≥{:.1f}")
        slider_row("MACD 시그널 기간",        smrh["macd_signal"], 9.0,  70.0,  1.0, "signal={:.0f}")
        self._param_vars["smrh_stop"] = smrh
        reentry_row("smrh_stop")

        # 여백
        tk.Frame(inner, bg=C.BG2, height=12).pack()

    def _apply_strategy_params(self) -> None:
        """파라미터 탭 슬라이더 값을 전략 모듈 변수에 즉시 반영."""
        if not self._param_vars:
            return
        p = self._param_vars
        log = logging.getLogger(__name__)
        try:
            # ── VB → config.* + 모듈 변수 ──
            import strategies.vb_noise_filter as _vbf
            import strategies.vb_standard     as _vbs
            vb = p.get("vb", {})
            if "noise_filter_days" in vb:
                config.NOISE_FILTER_DAYS = int(vb["noise_filter_days"].get())
            if "ma_period" in vb:
                config.MA_PERIOD = int(vb["ma_period"].get())
            if "k_min" in vb:
                val = float(vb["k_min"].get())
                _vbf._K_MIN = val; _vbs._K_MIN = val
            if "k_max" in vb:
                val = float(vb["k_max"].get())
                _vbf._K_MAX = val; _vbs._K_MAX = val
            if "time_cut_hours" in vb:
                val = float(vb["time_cut_hours"].get())
                _vbf._TIME_CUT_HOURS = val; _vbs._TIME_CUT_HOURS = val
            if "min_momentum_pct" in vb:
                val = float(vb["min_momentum_pct"].get())
                _vbf._MIN_MOMENTUM_PCT = val; _vbs._MIN_MOMENTUM_PCT = val
            if "vol_mult" in vb:
                val = float(vb["vol_mult"].get())
                _vbf._VOL_MULT = val; _vbs._VOL_MULT = val

            # ── mr_rsi ──
            import strategies.mr_rsi as _mr_rsi
            rsi = p.get("mr_rsi", {})
            if "rsi_buy"        in rsi: _mr_rsi._RSI_BUY        = float(rsi["rsi_buy"].get())
            if "rsi_buy_range"  in rsi: _mr_rsi._RSI_BUY_RANGE  = float(rsi["rsi_buy_range"].get())
            if "rsi_sell"       in rsi: _mr_rsi._RSI_SELL       = float(rsi["rsi_sell"].get())
            if "adx_range_thr"  in rsi: _mr_rsi._ADX_RANGE_THR  = float(rsi["adx_range_thr"].get())
            if "max_hold_hours" in rsi: _mr_rsi._MAX_HOLD_HOURS = float(rsi["max_hold_hours"].get())

            # ── mr_bollinger ──
            import strategies.mr_bollinger as _mr_bol
            bb = p.get("mr_bollinger", {})
            if "rsi_buy"       in bb: _mr_bol._RSI_BUY        = float(bb["rsi_buy"].get())
            if "adx_limit"     in bb: _mr_bol._ADX_LIMIT      = float(bb["adx_limit"].get())
            if "bb_period"     in bb: _mr_bol._BB_PERIOD      = int(bb["bb_period"].get())
            if "bb_std_trend"  in bb: _mr_bol._BB_STD_TREND   = float(bb["bb_std_trend"].get())
            if "bb_std_range"  in bb: _mr_bol._BB_STD_RANGE   = float(bb["bb_std_range"].get())
            if "adx_range_thr" in bb: _mr_bol._ADX_RANGE_THR  = float(bb["adx_range_thr"].get())
            if "max_hold_hours"in bb: _mr_bol._MAX_HOLD_HOURS = float(bb["max_hold_hours"].get())

            # ── scalping_triple_ema ──
            import strategies.scalping_triple_ema as _ste
            ema = p.get("scalping_triple_ema", {})
            if "tp_pct"         in ema: _ste._TP_PCT         = float(ema["tp_pct"].get()) / 100.0
            if "sl_pct"         in ema: _ste._SL_PCT         = float(ema["sl_pct"].get()) / 100.0
            if "adx_min"        in ema: _ste._ADX_MIN        = float(ema["adx_min"].get())
            if "ema_spread_min" in ema: _ste._EMA_SPREAD_MIN = float(ema["ema_spread_min"].get())
            if "trail_min_pct"  in ema: _ste._TRAIL_MIN_PCT  = float(ema["trail_min_pct"].get()) / 100.0

            # ── scalping_bb_rsi ──
            import strategies.scalping_bb_rsi as _sbb
            sbb = p.get("scalping_bb_rsi", {})
            if "rsi_buy"   in sbb: _sbb._RSI_BUY   = float(sbb["rsi_buy"].get())
            if "adx_limit" in sbb: _sbb._ADX_LIMIT = float(sbb["adx_limit"].get())
            if "atr_mult"  in sbb: _sbb._ATR_MULT  = float(sbb["atr_mult"].get())

            # ── scalping_5ema_reversal ──
            import strategies.scalping_5ema_reversal as _s5
            e5 = p.get("scalping_5ema_reversal", {})
            if "rr_ratio"         in e5: _s5._RR               = float(e5["rr_ratio"].get())
            if "adx_min"          in e5: _s5._ADX_MIN          = float(e5["adx_min"].get())
            if "rsi_entry_max"    in e5: _s5._RSI_ENTRY_MAX    = float(e5["rsi_entry_max"].get())
            if "vol_mult"         in e5: _s5._VOL_MULT         = float(e5["vol_mult"].get())
            if "time_cut_min"     in e5: _s5._TIME_CUT_MIN     = float(e5["time_cut_min"].get())
            if "min_momentum_pct" in e5: _s5._MIN_MOMENTUM_PCT = float(e5["min_momentum_pct"].get())

            # ── macd_rsi_trend ──
            import strategies.macd_rsi_trend as _mrt
            mrt = p.get("macd_rsi_trend", {})
            if "rsi_entry_min" in mrt: _mrt._RSI_ENTRY_MIN = float(mrt["rsi_entry_min"].get())
            if "rsi_sl"        in mrt: _mrt._RSI_SL        = float(mrt["rsi_sl"].get())
            if "vol_mult"      in mrt: _mrt._VOL_MULT      = float(mrt["vol_mult"].get())

            # ── smrh_stop ──
            import strategies.smrh_stop as _smrh
            smrh = p.get("smrh_stop", {})
            if "rsi_min"     in smrh: _smrh._RSI_MIN     = float(smrh["rsi_min"].get())
            if "macd_signal" in smrh: _smrh._MACD_SIGNAL = int(smrh["macd_signal"].get())

            log.info(
                f"[파라미터 적용] "
                f"VB(noise={config.NOISE_FILTER_DAYS}d MA={config.MA_PERIOD}"
                f" K=[{_vbf._K_MIN:.2f}~{_vbf._K_MAX:.2f}]"
                f" timecut={_vbf._TIME_CUT_HOURS:.1f}h/{_vbf._MIN_MOMENTUM_PCT:.1f}%"
                f" vol≥×{_vbf._VOL_MULT:.1f}) | "
                f"mr_rsi(RSI<{_mr_rsi._RSI_BUY:.0f}or{_mr_rsi._RSI_BUY_RANGE:.0f}"
                f" >{_mr_rsi._RSI_SELL:.0f} hold={_mr_rsi._MAX_HOLD_HOURS:.0f}h) | "
                f"mr_bol(RSI<{_mr_bol._RSI_BUY:.0f} ADX<{_mr_bol._ADX_LIMIT:.0f}"
                f" std={_mr_bol._BB_STD_RANGE:.1f}/{_mr_bol._BB_STD_TREND:.1f}"
                f" hold={_mr_bol._MAX_HOLD_HOURS:.0f}h) | "
                f"triple_ema(TP={_ste._TP_PCT:.1%} SL={_ste._SL_PCT:.1%}"
                f" ADX≥{_ste._ADX_MIN:.0f} spread≥{_ste._EMA_SPREAD_MIN:.1f}%"
                f" trail≥{_ste._TRAIL_MIN_PCT:.1%}) | "
                f"bb_rsi(RSI<{_sbb._RSI_BUY:.0f} ADX<{_sbb._ADX_LIMIT:.0f} ATR×{_sbb._ATR_MULT:.1f}) | "
                f"5ema(RR=1:{_s5._RR:.1f} ADX≥{_s5._ADX_MIN:.0f}"
                f" RSI<{_s5._RSI_ENTRY_MAX:.0f} vol≥×{_s5._VOL_MULT:.1f}"
                f" timecut={_s5._TIME_CUT_MIN:.0f}m/{_s5._MIN_MOMENTUM_PCT:.1f}%) | "
                f"macd_rsi(골든크로스(제로하) RSI>{_mrt._RSI_ENTRY_MIN:.1f}"
                f" SL<{_mrt._RSI_SL:.1f} vol×{_mrt._VOL_MULT:.1f}) | "
                f"smrh(RSI≥{_smrh._RSI_MIN:.1f} sig={_smrh._MACD_SIGNAL} HA강한양봉)"
            )
        except Exception as e:
            logging.getLogger(__name__).warning(f"전략 파라미터 적용 실패: {e}")

    def _build_notif_tab(self, parent: tk.Frame) -> None:
        def sep(): tk.Frame(parent, bg=C.BG3, height=1).pack(fill="x", padx=10, pady=(10, 0))
        def hdr(t): tk.Label(parent, text=t, font=("Arial", 9, "bold"),
                              fg=C.ACCENT, bg=C.BG2).pack(anchor="w", padx=12, pady=(4, 2))

        sep(); hdr("▶  옵시디언 볼트 경로")
        pf = tk.Frame(parent, bg=C.BG2); pf.pack(fill="x", padx=12, pady=4)
        self._vault_var = tk.StringVar(value=config.OBSIDIAN_VAULT_PATH)
        tk.Entry(pf, textvariable=self._vault_var, font=("Consolas", 8),
                 bg=C.BG3, fg=C.FG, insertbackground=C.FG,
                 relief="flat", bd=4).pack(side="left", fill="x", expand=True)
        tk.Button(pf, text="찾기", font=("Arial", 8),
                  bg=C.BG3, fg=C.FG, relief="flat", bd=2,
                  command=self._browse_vault).pack(side="left", padx=(4, 0))

        self._obsidian_enabled = tk.BooleanVar(value=bool(config.OBSIDIAN_VAULT_PATH))
        tk.Checkbutton(parent, text="옵시디언 기록 활성화", variable=self._obsidian_enabled,
                        font=("Arial", 9), fg=C.FG, bg=C.BG2,
                        selectcolor=C.BG3, activebackground=C.BG2,
                        activeforeground=C.FG).pack(anchor="w", padx=12, pady=2)

        sep(); hdr("▶  요약 알림 주기")
        notif_options = ["1시간", "2시간", "3시간", "6시간", "12시간", "비활성화"]
        self._notif_var = tk.StringVar(value="3시간")
        ttk.Combobox(parent, textvariable=self._notif_var,
                     values=notif_options, state="readonly",
                     font=("Arial", 9)).pack(fill="x", padx=12, pady=4)

        tk.Button(parent, text="지금 요약 전송", font=("Arial", 9),
                  bg=C.BG3, fg=C.FG, relief="flat", bd=2, pady=4,
                  command=self._send_summary_now).pack(fill="x", padx=12, pady=4)

        sep(); hdr("▶  Gemini AI 전략 분석")
        gf = tk.Frame(parent, bg=C.BG2); gf.pack(fill="x", padx=12, pady=(4, 2))
        tk.Label(gf, text="API Key:", font=("Arial", 8), fg=C.FG, bg=C.BG2,
                 width=7, anchor="w").pack(side="left")
        self._gemini_key_var = tk.StringVar(value=config.GEMINI_API_KEY or "")
        tk.Entry(gf, textvariable=self._gemini_key_var, font=("Consolas", 8),
                 bg=C.BG3, fg=C.FG, insertbackground=C.FG,
                 show="*", relief="flat", bd=4,
                 ).pack(side="left", fill="x", expand=True, padx=(4, 0))

        self._gemini_btn = tk.Button(
            parent, text="🔍  Gemini 전략 분석 실행",
            font=("Arial", 9, "bold"),
            bg=C.BG3, fg=C.ACCENT, relief="flat", bd=0, pady=5,
            cursor="hand2", command=self._on_gemini_analyze,
        )
        self._gemini_btn.pack(fill="x", padx=12, pady=(2, 2))
        tk.Label(parent,
                 text="※ google-generativeai 설치 필요: pip install google-generativeai\n"
                      "   https://aistudio.google.com/app/apikey 에서 무료 발급",
                 font=("Arial", 7), fg=C.SUB, bg=C.BG2,
                 justify="left").pack(anchor="w", padx=14, pady=(0, 4))

        sep()
        tk.Label(parent, text="※ plyer 설치 시 Windows 알림 지원\n   pip install plyer",
                 font=("Arial", 8), fg=C.SUB, bg=C.BG2,
                 justify="left").pack(anchor="w", padx=12, pady=6)

    # ─── 우측 패널 ───────────────────────────────────────────────────────────

    def _build_right_panel(self, parent: tk.Frame) -> None:
        nb = ttk.Notebook(parent)
        nb.pack(fill="both", expand=True)
        self._right_nb = nb

        # 로그 탭
        lf = tk.Frame(nb, bg=C.BG); nb.add(lf, text="  로그  ")
        self._log_text = scrolledtext.ScrolledText(
            lf, font=("Consolas", 9), bg="#181825", fg=C.FG,
            insertbackground="white", relief="flat", bd=0, state="disabled",
        )
        self._log_text.pack(fill="both", expand=True, padx=2, pady=2)
        self._log_text.tag_config("INFO",     foreground=C.FG)
        self._log_text.tag_config("DEBUG",    foreground=C.SUB)
        self._log_text.tag_config("WARNING",  foreground=C.YELLOW)
        self._log_text.tag_config("ERROR",    foreground=C.RED)
        self._log_text.tag_config("CRITICAL", foreground="#ff5555",
                                   font=("Consolas", 9, "bold"))

        # 로그 필터 버튼 바
        log_bf = tk.Frame(lf, bg=C.BG)
        log_bf.pack(fill="x", padx=2, pady=2)
        self._log_filter_level = tk.StringVar(value="ALL")
        for lvl, txt, clr in [("ALL", "전체", C.FG),
                                ("WARNING", "⚠ WARN+", C.YELLOW),
                                ("ERROR", "❌ ERROR", C.RED)]:
            tk.Radiobutton(
                log_bf, text=txt, variable=self._log_filter_level, value=lvl,
                font=("Arial", 8, "bold"), fg=clr, bg=C.BG,
                selectcolor=C.BG3, activebackground=C.BG, activeforeground=clr,
                indicatoron=False, padx=8, pady=2, relief="flat", bd=1,
                command=self._apply_log_filter,
            ).pack(side="left", padx=2)
        tk.Button(log_bf, text="지우기", font=("Arial", 8), bg=C.BG3, fg=C.FG,
                  relief="flat", bd=2, command=self._clear_log
                  ).pack(side="right", padx=4)

        # 실제 포지션 탭
        pf = tk.Frame(nb, bg=C.BG); nb.add(pf, text="  실제 포지션  ")
        self._real_pos_tree = self._make_tree(pf, [
            ("ticker",    "종목",     90),
            ("buy_price", "매수가",   120),
            ("volume",    "수량",     140),
            ("stop_loss", "손절가",   120),
            ("pnl",       "평가손익", 100),
        ])
        tk.Label(pf, text="* 3초 갱신", font=("Arial", 8),
                 fg=C.SUB, bg=C.BG).pack(anchor="e", padx=8, pady=2)

        # 가상계좌 현황 탭
        vf = tk.Frame(nb, bg=C.BG); nb.add(vf, text="  가상계좌 현황  ")
        self._paper_tree = self._make_tree(vf, [
            ("account",    "계좌명",    90),
            ("scenario",   "전략",     140),
            ("ticker_cnt", "종목수",    55),
            ("equity",     "현재평가", 110),
            ("pnl",        "손익",      95),
            ("pnl_pct",    "수익률",    75),
            ("trades",     "거래수",    60),
            ("win_rate",   "승률",      60),
        ])
        self._paper_tree.bind("<ButtonRelease-1>", self._on_paper_account_click)
        tk.Label(vf, text="* 3초 갱신  │  계좌 행 클릭 → 거래 로그", font=("Arial", 8),
                 fg=C.SUB, bg=C.BG).pack(anchor="e", padx=8, pady=2)

    def _make_tree(self, parent: tk.Frame, cols: list[tuple]) -> ttk.Treeview:
        tree = ttk.Treeview(parent, columns=[c[0] for c in cols],
                             show="headings", selectmode="none")
        for col, text, width in cols:
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor="center")
        tree.tag_configure("profit", foreground=C.GREEN)
        tree.tag_configure("loss",   foreground=C.RED)
        tree.pack(fill="both", expand=True, padx=4, pady=4)
        return tree

    # ─── 상태 인디케이터 ──────────────────────────────────────────────────────

    def _update_status_dots(self) -> None:
        """상단바 실제/가상 상태 인디케이터 갱신"""
        self._real_dot.config(fg=C.GREEN if self._real_running else C.RED)
        self._real_lbl.config(
            text="실제: 실행 중" if self._real_running else "실제: 정지",
            fg=C.PEACH if self._real_running else C.FG,
        )
        self._paper_dot.config(fg=C.GREEN if self._paper_running else C.RED)
        self._paper_lbl.config(
            text="가상: 실행 중" if self._paper_running else "가상: 정지",
            fg=C.ACCENT if self._paper_running else C.FG,
        )
        # 둘 다 정지 시 자산/타이머 초기화
        if not self._real_running and not self._paper_running:
            self._equity_label.config(text="자산: —")
            self._timer_label.config(text="⏱ —")

    # ─── 공통 설정 수집 ───────────────────────────────────────────────────────

    def _collect_settings(self) -> bool:
        """설정 검증 및 config 반영. 실패 시 False 반환."""
        tickers = [t for t, v in self._ticker_vars.items() if v.get()]
        if not tickers:
            messagebox.showwarning("경고", "거래 종목을 하나 이상 선택하세요.")
            return False
        try:
            budget = int(self._budget_var.get().replace(",", ""))
            assert budget >= config.MIN_ORDER_KRW
        except Exception:
            messagebox.showwarning("경고", f"예산은 {config.MIN_ORDER_KRW:,}원 이상이어야 합니다.")
            return False

        strat_id, scen_id = STRATEGIES[self._strategy_var.get()]
        config.SELECTED_STRATEGY = strat_id
        config.SELECTED_SCENARIO = scen_id
        config.TICKERS           = tickers
        config.BUDGET_PER_TRADE  = budget
        config.STOP_LOSS_PCT     = self._stoploss_var.get() / 100
        config.MAX_DRAWDOWN_PCT  = self._drawdown_var.get() / 100
        return True

    def _init_obsidian_notif(self) -> None:
        """옵시디언 + 알림 매니저 초기화 (아직 없을 때만 생성)"""
        if self._obsidian is None:
            vault = self._vault_var.get().strip()
            # vault 미지정 시 기본 경로(logs/obsidian/) 자동 사용 — 항상 생성
            self._obsidian = ObsidianLogger(vault, config.OBSIDIAN_FOLDER)
        if self._notif_mgr is None:
            notif_map = {"1시간": 1, "2시간": 2, "3시간": 3, "6시간": 6, "12시간": 12}
            notif_val = self._notif_var.get()
            self._notif_mgr = (
                NotificationManager(notif_map[notif_val], self._obsidian)
                if notif_val in notif_map else None
            )

    def _ensure_session_timer(self) -> None:
        """세션 타이머가 없을 때만 새로 시작"""
        if self._session_timer is None:
            duration_sec = DURATION_OPTIONS.get(self._session_var.get())
            self._session_timer = SessionTimer(duration_sec, on_expire=self._on_session_expire)
            self._session_timer.start()

    def _cleanup_if_all_stopped(self) -> None:
        """실제 + 가상 모두 정지 시 공용 리소스 정리"""
        if not self._real_running and not self._paper_running:
            if self._session_timer:
                self._session_timer.stop()
                self._session_timer = None
            if self._notif_mgr:
                self._notif_mgr.stop()
                self._notif_mgr = None
            self._obsidian = None

    # ─── 실제거래 시작 / 정지 ────────────────────────────────────────────────

    def _on_start_real(self) -> None:
        if self._real_running:
            return
        if not self._collect_settings():
            return
        if not config.ACCESS_KEY or not config.SECRET_KEY:
            messagebox.showerror("오류", ".env 파일에 API 키가 설정되어 있지 않습니다.")
            return

        self._apply_strategy_params()
        self._init_obsidian_notif()
        self._real_running = True
        self._real_start_btn.config(state="disabled")
        self._real_stop_btn.config(state="normal")
        self._update_status_dots()
        self._ensure_session_timer()
        self._start_real()

    def _on_stop_real(self) -> None:
        self._real_stop_btn.config(state="disabled", text="■  정지 중...")
        threading.Thread(target=self._do_stop_real, daemon=True).start()

    def _do_stop_real(self) -> None:
        if self._trader:
            self._trader.stop()
        self._real_running = False
        self._cleanup_if_all_stopped()
        self.after(0, self._on_real_stopped)

    def _on_real_stopped(self) -> None:
        self._trader = None
        self._real_running = False
        self._real_start_btn.config(state="normal")
        self._real_stop_btn.config(state="disabled", text="■  정지")
        self._update_status_dots()

    # ─── 가상거래 시작 / 정지 ────────────────────────────────────────────────

    def _on_start_paper_btn(self) -> None:
        if self._paper_running:
            return
        if not self._paper_rows:
            messagebox.showwarning("경고", "가상계좌 탭에서 계좌를 추가하세요.")
            return

        # 손절/낙폭만 config에 반영 (종목은 시나리오별 독립)
        config.STOP_LOSS_PCT    = self._stoploss_var.get() / 100
        config.MAX_DRAWDOWN_PCT = self._drawdown_var.get() / 100
        self._apply_strategy_params()
        self._init_obsidian_notif()
        self._paper_running = True
        self._paper_start_btn.config(state="disabled")
        self._paper_stop_btn.config(state="normal")
        self._update_status_dots()
        self._ensure_session_timer()
        self._start_paper()

    def _on_stop_paper_btn(self) -> None:
        self._paper_stop_btn.config(state="disabled", text="■  정지 중...")
        threading.Thread(target=self._do_stop_paper, daemon=True).start()

    def _do_stop_paper(self) -> None:
        if self._paper_engine:
            self._paper_engine.stop()
        if hasattr(self, "_paper_ws"):
            self._paper_ws.stop()
        self._paper_running = False
        self._cleanup_if_all_stopped()
        self.after(0, self._on_paper_stopped)

    def _on_paper_stopped(self) -> None:
        self._paper_engine = None
        self._paper_running = False
        self._paper_start_btn.config(state="normal")
        self._paper_stop_btn.config(state="disabled", text="■  정지")
        self._update_status_dots()

    def _on_session_expire(self) -> None:
        logging.getLogger(__name__).info("세션 시간 만료 → 자동 정지")
        if self._real_running:
            self.after(0, self._on_stop_real)
        if self._paper_running:
            self.after(0, self._on_stop_paper_btn)

    # ─── 실제거래 내부 실행 ──────────────────────────────────────────────────

    def _start_real(self) -> None:
        self._trader_thread = threading.Thread(
            target=self._run_real_trader, daemon=True, name="RealTrader"
        )
        self._trader_thread.start()

    def _run_real_trader(self) -> None:
        try:
            from core.trader import Trader
            self._trader = Trader()
            if self._obsidian:
                self._trader.obsidian_logger = self._obsidian
            if self._notif_mgr:
                self._notif_mgr.set_summary_provider(self._get_real_summary)
                self._notif_mgr.start()
            self._trader.start()
        except SystemExit:
            pass
        except Exception as e:
            logging.getLogger(__name__).critical(f"실거래 오류: {e}", exc_info=True)
        finally:
            self._trader = None
            self._real_running = False
            self.after(0, self._on_real_stopped)

    def _get_real_summary(self) -> list[dict]:
        if not self._trader:
            return []
        try:
            equity = self._trader.risk.get_total_equity()
            return [{
                "account_id":      "REAL",
                "scenario_id":     config.SELECTED_SCENARIO,
                "initial_balance": 0,
                "current_equity":  equity,
                "total_pnl":       0,
                "total_pnl_pct":   0,
                "total_trades":    0,
                "win_rate":        0,
                "is_paper":        False,
            }]
        except Exception:
            return []

    # ─── 가상거래 내부 실행 ──────────────────────────────────────────────────

    def _start_paper(self) -> None:
        from core.paper_account import PaperAccount
        from core.paper_engine import PaperEngine, PaperScenario
        from data.market_data import MarketData
        from exchange.websocket_manager import WebSocketManager, PriceCache

        market_data = MarketData()
        price_cache = PriceCache()

        # 종목 수별 캐시 (동일 N은 1회만 API 호출)
        _ticker_cache: dict[int, list[str]] = {}
        def _get_top(n: int) -> list[str]:
            if n not in _ticker_cache:
                _ticker_cache[n] = MarketData.get_top_tickers_by_volume(n)
            return _ticker_cache[n]

        all_tickers: set[str] = set()
        scenarios: list[PaperScenario] = []

        for row in self._paper_rows:
            try:
                balance = float(row.balance_var.get().replace(",", ""))
            except ValueError:
                balance = float(config.PAPER_DEFAULT_BALANCE)

            try:
                ticker_count = int(row.ticker_count_var.get())
            except ValueError:
                ticker_count = config.PAPER_DEFAULT_TICKER_COUNT

            try:
                budget_pt = int(row.budget_per_trade_var.get().replace(",", ""))
            except ValueError:
                budget_pt = config.PAPER_DEFAULT_BUDGET_PER_TRADE

            strat_key = row.strategy_var.get()
            strat_id, scen_id = STRATEGIES.get(
                strat_key, (config.SELECTED_STRATEGY, config.SELECTED_SCENARIO)
            )

            scenario_tickers = _get_top(ticker_count)
            all_tickers.update(scenario_tickers)

            account = PaperAccount(
                account_id=row.name_var.get() or row.account_id,
                scenario_id=scen_id,
                initial_balance=balance,
            )
            scenario = PaperScenario(
                account, market_data, strat_id, scen_id,
                tickers=scenario_tickers,
                budget_per_trade=budget_pt,
            )
            scenarios.append(scenario)

        ws = WebSocketManager(list(all_tickers), price_cache)
        ws.start()
        self._paper_ws = ws

        engine = PaperEngine(
            scenarios=scenarios,
            market_data=market_data,
            price_cache=price_cache,
            tickers=list(all_tickers),
            obsidian_logger=self._obsidian,
        )
        self._paper_engine = engine

        if self._notif_mgr:
            self._notif_mgr.set_summary_provider(self._get_paper_summaries)
            self._notif_mgr.start()

        engine.start()
        logging.getLogger(__name__).info(
            f"가상거래 시작 | {len(scenarios)}개 계좌 | 전체 종목: {len(all_tickers)}개"
        )

    def _get_paper_summaries(self) -> list[dict]:
        if not self._paper_engine:
            return []
        summaries = self._paper_engine.get_all_summaries()
        for s in summaries:
            s["is_paper"] = True
        return summaries

    # ─── 상태 폴링 ───────────────────────────────────────────────────────────

    def _poll_status(self) -> None:
        if self._real_running or self._paper_running:
            # 타이머
            if self._session_timer:
                self._timer_label.config(text=f"⏱ {self._session_timer.remaining_str()}")

            # 실제거래: 자산 + 포지션
            if self._trader:
                try:
                    equity = self._trader.risk.get_total_equity()
                    self._equity_label.config(text=f"실제자산: {equity:,.0f}원")
                    self._update_real_positions(self._trader.state.all_positions())
                except Exception:
                    pass

            # 가상거래: 현황 테이블 + 가상자산 (실거래 없을 때 topbar에도 표시)
            if self._paper_engine:
                try:
                    summaries = self._paper_engine.get_all_summaries()
                    self._update_paper_table(summaries)
                    if not self._trader:
                        total_equity = sum(s["current_equity"] for s in summaries)
                        self._equity_label.config(text=f"가상자산: {total_equity:,.0f}원")
                except Exception:
                    pass

        self.after(3000, self._poll_status)

    def _update_real_positions(self, positions: list) -> None:
        for row in self._real_pos_tree.get_children():
            self._real_pos_tree.delete(row)
        for pos in positions:
            try:
                price = self._trader.price_cache.get(pos.ticker) or pos.buy_price
                pnl_pct = (price - pos.buy_price) / pos.buy_price * 100
                pnl_str = f"{pnl_pct:+.2f}%"
                tag = "profit" if pnl_pct >= 0 else "loss"
            except Exception:
                pnl_str, tag = "—", ""
            self._real_pos_tree.insert("", "end", tags=(tag,), values=(
                pos.ticker, f"{pos.buy_price:,.0f}", f"{pos.volume:.8f}",
                f"{pos.stop_loss_price:,.0f}", pnl_str,
            ))

    def _update_paper_table(self, summaries: list[dict]) -> None:
        for row in self._paper_tree.get_children():
            self._paper_tree.delete(row)
        for s in summaries:
            tag = "profit" if s["total_pnl"] >= 0 else "loss"
            # 시나리오의 종목 수 조회
            ticker_cnt = "—"
            if self._paper_engine:
                sc = self._paper_engine.get_scenario(s["account_id"])
                if sc:
                    ticker_cnt = str(len(sc.tickers))
            self._paper_tree.insert("", "end", tags=(tag,), values=(
                s["account_id"],
                s["scenario_id"],
                ticker_cnt,
                f"{s['current_equity']:,.0f}",
                f"{s['total_pnl']:+,.0f}",
                f"{s['total_pnl_pct']:+.2f}%",
                s["total_trades"],
                f"{s['win_rate']:.1f}%",
            ))

    # ─── 가상계좌 거래 로그 팝업 ──────────────────────────────────────────────

    def _on_paper_account_click(self, event) -> None:
        """가상계좌 현황 행 클릭 → 거래 로그 창 열기"""
        item = self._paper_tree.identify_row(event.y)
        if not item:
            return
        vals = self._paper_tree.item(item, "values")
        if not vals:
            return
        account_id  = vals[0]
        scenario_id = vals[1]
        self._show_trade_log_window(account_id, scenario_id)

    def _show_trade_log_window(self, account_id: str, scenario_id: str) -> None:
        """계좌별 거래 로그 팝업 창 생성 (이미 열려있으면 맨 앞으로)"""
        # 이미 열린 창이 있으면 맨 앞으로
        if account_id in self._trade_log_wins:
            win = self._trade_log_wins[account_id]
            if win.winfo_exists():
                win.lift(); win.focus_force(); return
            else:
                del self._trade_log_wins[account_id]

        if not self._paper_engine:
            from tkinter import messagebox
            messagebox.showinfo("안내", "가상거래가 실행 중이 아닙니다.", parent=self)
            return
        scenario = self._paper_engine.get_scenario(account_id)
        if not scenario:
            from tkinter import messagebox
            messagebox.showinfo("안내", f"계좌를 찾을 수 없습니다: {account_id}", parent=self)
            return

        # ── 창 생성 ──
        win = tk.Toplevel(self)
        win.title(f"거래 로그  │  {account_id}  ({scenario_id})")
        win.geometry("1000x540")
        win.minsize(720, 380)
        win.configure(bg=C.BG)
        self._trade_log_wins[account_id] = win
        win.protocol("WM_DELETE_WINDOW", lambda: self._close_trade_log_win(account_id))

        # ── 헤더 프레임 (버튼은 tree 생성 후 추가) ──
        hf = tk.Frame(win, bg=C.BG2, padx=10, pady=6)
        tk.Label(hf,
                 text=f"계좌: {account_id}  │  전략: {scenario_id}",
                 font=("Arial", 10, "bold"), fg=C.ACCENT, bg=C.BG2,
                 ).pack(side="left")
        lbl_summary = tk.Label(hf, text="", font=("Arial", 9), fg=C.FG, bg=C.BG2)
        lbl_summary.pack(side="left", padx=16)

        # ── 거래 테이블 ──
        tf = tk.Frame(win, bg=C.BG)
        log_tree = ttk.Treeview(
            tf,
            columns=["no", "time", "ticker", "action",
                     "price", "volume", "amount", "pnl", "balance"],
            show="headings",
            selectmode="none",
        )
        for col, text, width in [
            ("no",      "번호",   48),
            ("time",    "시간",  148),
            ("ticker",  "종목",   80),
            ("action",  "구분",   55),
            ("price",   "체결가", 115),
            ("volume",  "수량",  130),
            ("amount",  "거래금액", 115),
            ("pnl",     "손익",   95),
            ("balance", "잔고",  115),
        ]:
            log_tree.heading(col, text=text)
            log_tree.column(col, width=width, anchor="center")

        log_tree.tag_configure("buy",       foreground=C.ACCENT)  # 파랑 — 매수
        log_tree.tag_configure("sell_win",  foreground=C.GREEN)   # 초록 — 익절
        log_tree.tag_configure("sell_loss", foreground=C.RED)     # 빨강 — 손절

        sc = ttk.Scrollbar(tf, orient="vertical", command=log_tree.yview)
        log_tree.configure(yscrollcommand=sc.set)
        log_tree.pack(side="left", fill="both", expand=True)
        sc.pack(side="right", fill="y")

        # ── 새로고침 버튼 (tree 생성 후 lambda에서 참조) ──
        tk.Button(
            hf, text="↻ 새로고침", font=("Arial", 8),
            bg=C.BG3, fg=C.FG, relief="flat", bd=2, padx=6, cursor="hand2",
            command=lambda: self._refresh_trade_log(
                win, account_id, lbl_summary, log_tree),
        ).pack(side="right")

        # ── 레이아웃 pack (헤더 상단, 테이블 하단) ──
        hf.pack(fill="x")
        tf.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        # 초기 로드 + 3초 자동 갱신 시작
        self._refresh_trade_log(win, account_id, lbl_summary, log_tree)
        win.after(3000, lambda: self._schedule_trade_log_refresh(
            win, account_id, lbl_summary, log_tree))

    def _close_trade_log_win(self, account_id: str) -> None:
        win = self._trade_log_wins.pop(account_id, None)
        if win and win.winfo_exists():
            win.destroy()

    def _refresh_trade_log(
        self,
        win:         tk.Toplevel,
        account_id:  str,
        lbl_summary: tk.Label,
        tree:        ttk.Treeview,
    ) -> None:
        """거래 로그 창 내용 갱신"""
        if not win.winfo_exists():
            return
        if not self._paper_engine:
            return
        scenario = self._paper_engine.get_scenario(account_id)
        if not scenario:
            return

        trades = scenario.account.trade_history
        sells  = [t for t in trades if t.action == "SELL"]
        buys   = [t for t in trades if t.action == "BUY"]
        wins   = [t for t in sells if t.pnl > 0]
        total_pnl = sum(t.pnl for t in sells)
        win_rate  = len(wins) / len(sells) * 100 if sells else 0.0

        lbl_summary.config(
            text=(
                f"총 {len(trades)}건  (매수 {len(buys)}  /  매도 {len(sells)})  │  "
                f"승률 {win_rate:.1f}%  │  누적손익 {total_pnl:+,.0f}원"
            ),
            fg=C.GREEN if total_pnl >= 0 else C.RED,
        )

        # 기존 행 제거 후 최신순으로 다시 채우기
        for row in tree.get_children():
            tree.delete(row)

        for t in reversed(trades):
            if t.action == "BUY":
                tag     = "buy"
                pnl_str = "—"
            else:
                tag     = "sell_win" if t.pnl >= 0 else "sell_loss"
                pnl_str = f"{t.pnl:+,.0f}원"

            ts = t.timestamp
            time_str = (ts.strftime("%m/%d %H:%M:%S")
                        if hasattr(ts, "strftime") else str(ts)[:19])

            tree.insert("", "end", tags=(tag,), values=(
                t.trade_no,
                time_str,
                t.ticker.replace("KRW-", ""),
                "▲ 매수" if t.action == "BUY" else "▼ 매도",
                f"{t.price:,.0f}",
                f"{t.volume:.6f}",
                f"{t.amount_krw:,.0f}원",
                pnl_str,
                f"{t.balance_after:,.0f}원",
            ))

    def _schedule_trade_log_refresh(
        self,
        win:         tk.Toplevel,
        account_id:  str,
        lbl_summary: tk.Label,
        tree:        ttk.Treeview,
    ) -> None:
        """3초 주기 자동 갱신 스케줄러"""
        if not win.winfo_exists():
            return
        self._refresh_trade_log(win, account_id, lbl_summary, tree)
        win.after(3000, lambda: self._schedule_trade_log_refresh(
            win, account_id, lbl_summary, tree))

    # ─── 로그 ────────────────────────────────────────────────────────────────

    def _poll_logs(self) -> None:
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass
        self.after(100, self._poll_logs)

    def _append_log(self, msg: str) -> None:
        upper = msg.upper()
        tag = ("CRITICAL" if "[CRITIC" in upper else
               "ERROR"    if "[ERROR"  in upper else
               "WARNING"  if "[WARNIN" in upper else
               "DEBUG"    if "[DEBUG"  in upper else "INFO")
        self._log_text.config(state="normal")
        self._log_text.insert("end", msg + "\n", tag)
        self._log_text.see("end")
        lines = int(self._log_text.index("end-1c").split(".")[0])
        if lines > 2000:
            self._log_text.delete("1.0", "300.0")
        self._log_text.config(state="disabled")

    def _clear_log(self) -> None:
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")

    def _apply_log_filter(self) -> None:
        """로그 필터 레벨 변경 (QueueHandler 레벨 동적 조정)"""
        lvl = self._log_filter_level.get()
        level_map = {"ALL": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}
        target_level = level_map.get(lvl, logging.INFO)
        for h in logging.getLogger().handlers:
            if isinstance(h, _QueueHandler):
                h.setLevel(target_level)
                break

    # ─── 종목 선택 헬퍼 ──────────────────────────────────────────────────────

    def _select_all_tickers(self) -> None:
        """체크박스 목록 전체 선택"""
        for var in self._ticker_vars.values():
            var.set(True)

    def _deselect_all_tickers(self) -> None:
        """체크박스 목록 전체 해제"""
        for var in self._ticker_vars.values():
            var.set(False)

    def _update_ticker_sel_label(self) -> None:
        """선택된 종목 수 표시 갱신"""
        sel = sum(1 for v in self._ticker_vars.values() if v.get())
        total = len(self._ticker_vars)
        if total:
            self._ticker_sel_label.config(text=f"({sel}/{total} 선택)")
        else:
            self._ticker_sel_label.config(text="")

    # ─── 종목 동적 로드 ──────────────────────────────────────────────────────

    def _refresh_tickers(self) -> None:
        """거래량 상위 100개 종목을 백그라운드에서 조회해 체크박스 목록 갱신"""
        self._ticker_status.config(text="로딩 중...", fg=C.YELLOW)
        threading.Thread(target=self._fetch_tickers_bg, daemon=True).start()

    def _fetch_tickers_bg(self) -> None:
        try:
            from data.market_data import MarketData
            tickers = MarketData.get_top_tickers_by_volume(100)
            self.after(0, lambda: self._populate_tickers(tickers))
        except Exception as e:
            self.after(0, lambda err=e: self._ticker_status.config(
                text=f"로드 실패: {err}", fg=C.RED
            ))

    def _populate_tickers(self, tickers: list[str]) -> None:
        prev_selected = {t for t, v in self._ticker_vars.items() if v.get()}
        for w in self._ticker_inner.winfo_children():
            w.destroy()
        self._ticker_vars.clear()

        default_tickers = set(config.TICKERS)
        cols = 4
        for i, ticker in enumerate(tickers):
            checked = ticker in prev_selected or ticker in default_tickers
            var = tk.BooleanVar(value=checked)
            var.trace_add("write", lambda *_, v=var: self._update_ticker_sel_label())
            self._ticker_vars[ticker] = var
            cb = tk.Checkbutton(
                self._ticker_inner,
                text=ticker.replace("KRW-", ""),
                variable=var,
                font=("Arial", 8),
                fg=C.FG, bg=C.BG2,
                selectcolor=C.BG3,
                activebackground=C.BG2,
                activeforeground=C.FG,
            )
            cb.grid(row=i // cols, column=i % cols, sticky="w", padx=4, pady=1)
            # 체크박스 위에서도 마우스휠 스크롤 전달
            if hasattr(self, "_ticker_scroll_fn"):
                cb.bind("<MouseWheel>", self._ticker_scroll_fn)

        self._ticker_status.config(text=f"거래량 상위 {len(tickers)}개", fg=C.GREEN)
        self._update_ticker_sel_label()

    # ─── Gemini 전략 분석 ─────────────────────────────────────────────────────

    def _on_gemini_analyze(self) -> None:
        """Gemini 분석 버튼 클릭 → 백그라운드 실행"""
        api_key = self._gemini_key_var.get().strip()
        if not api_key:
            messagebox.showwarning("Gemini API Key 없음",
                                   "알림/기록 탭에서 Gemini API Key를 입력하세요.\n"
                                   "https://aistudio.google.com/app/apikey (무료)")
            return

        self._gemini_btn.config(state="disabled", text="⏳ 분석 중...", fg=C.YELLOW)
        scenario = config.SELECTED_SCENARIO
        threading.Thread(
            target=self._do_gemini_analyze,
            args=(api_key, scenario),
            daemon=True, name="GeminiAnalyze",
        ).start()

    def _do_gemini_analyze(self, api_key: str, scenario: str) -> None:
        """백그라운드: Gemini API 호출 및 분석"""
        try:
            from core.gemini_analyzer import GeminiStrategyAnalyzer
            analyzer = GeminiStrategyAnalyzer(api_key)
            result = analyzer.analyze(
                scenario_id=scenario,
                max_trades=config.GEMINI_MAX_TRADES,
            )
            self.after(0, lambda r=result: self._show_gemini_result(r))
        except ImportError:
            self.after(0, lambda: self._gemini_error(
                "google-generativeai 패키지가 없습니다.\n"
                "터미널에서: pip install google-generativeai"
            ))
        except Exception as e:
            self.after(0, lambda err=str(e): self._gemini_error(err))
        finally:
            self.after(0, lambda: self._gemini_btn.config(
                state="normal", text="🔍  Gemini 전략 분석 실행", fg=C.ACCENT
            ))

    def _gemini_error(self, msg: str) -> None:
        messagebox.showerror("Gemini 분석 실패", msg)

    def _show_gemini_result(self, result: dict) -> None:
        """Gemini 분석 결과 팝업 창 표시"""
        import json as _json

        win = tk.Toplevel(self)
        win.title(f"Gemini 전략 분석 결과 — {result.get('scenario_id', '')}")
        win.geometry("860x640")
        win.minsize(700, 500)
        win.configure(bg=C.BG)

        # 헤더 정보
        hf = tk.Frame(win, bg=C.BG2, padx=12, pady=8)
        hf.pack(fill="x")
        stats = (
            f"시나리오: {result.get('scenario_id')}  │  "
            f"거래 수: {result.get('trade_count')}건  │  "
            f"승률: {result.get('win_rate')}%  │  "
            f"평균수익: {result.get('avg_pnl_pct'):+.3f}%  │  "
            f"최고: {result.get('best_pnl_pct'):+.2f}%  "
            f"최저: {result.get('worst_pnl_pct'):+.2f}%"
        )
        tk.Label(hf, text=stats, font=("Arial", 9), fg=C.FG, bg=C.BG2).pack(anchor="w")
        tk.Label(hf, text=f"분석 시각: {result.get('analysis_timestamp', '')[:19]}",
                 font=("Arial", 8), fg=C.SUB, bg=C.BG2).pack(anchor="w")

        # 탭
        nb = ttk.Notebook(win); nb.pack(fill="both", expand=True, padx=8, pady=8)

        def _tab_text(title: str, content: str, highlight_color: str = C.FG) -> None:
            """스크롤 가능 텍스트 탭 추가"""
            f = tk.Frame(nb, bg=C.BG); nb.add(f, text=f"  {title}  ")
            txt = scrolledtext.ScrolledText(
                f, font=("Consolas", 9), bg="#181825", fg=highlight_color,
                insertbackground="white", relief="flat", bd=0, wrap="word",
            )
            txt.insert("1.0", content)
            txt.config(state="disabled")
            txt.pack(fill="both", expand=True, padx=4, pady=4)
            return txt

        # 탭 1: 문제점 & 개선안
        issues_txt = "\n".join(f"⚠  {i}" for i in result.get("issues", []))
        improv_txt = "\n".join(f"✅  {i}" for i in result.get("improvements", []))
        summary_content = (
            f"=== 발견된 문제점 ===\n{issues_txt or '없음'}\n\n"
            f"=== 개선 제안 ===\n{improv_txt or '없음'}"
        )
        _tab_text("문제점 & 개선안", summary_content, C.YELLOW)

        # 탭 2: Gemini 원문 분석
        _tab_text("Gemini 원문", result.get("gemini_analysis", ""), C.FG)

        # 탭 3: Claude 프롬프트 JSON (복사/저장 가능)
        claude_json_str = _json.dumps(
            result.get("claude_prompt_json", {}), ensure_ascii=False, indent=2
        )
        _tab_text("Claude 프롬프트 (JSON)", claude_json_str, C.ACCENT)

        # 하단 버튼
        bf = tk.Frame(win, bg=C.BG); bf.pack(fill="x", padx=8, pady=(0, 8))

        def _copy_claude_json():
            win.clipboard_clear()
            win.clipboard_append(claude_json_str)
            messagebox.showinfo("복사 완료",
                                "Claude 프롬프트 JSON이 클립보드에 복사됐습니다.\n"
                                "Claude Code 또는 claude.ai에 붙여넣기 하세요.",
                                parent=win)

        def _save_to_file():
            import os, json as _j
            from datetime import datetime
            from zoneinfo import ZoneInfo
            ts = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d_%H%M%S")
            fname = f"gemini_analysis_{result.get('scenario_id', 'unknown')}_{ts}.json"
            save_path = os.path.join(config.LOGS_DIR, fname)
            try:
                with open(save_path, "w", encoding="utf-8") as f:
                    _j.dump(result, f, ensure_ascii=False, indent=2)
                messagebox.showinfo("저장 완료", f"저장됨: {save_path}", parent=win)
            except Exception as e:
                messagebox.showerror("저장 실패", str(e), parent=win)

        tk.Button(bf, text="📋  Claude JSON 복사", font=("Arial", 9, "bold"),
                  bg=C.ACCENT, fg=C.HEADER, relief="flat", bd=0, padx=12, pady=5,
                  cursor="hand2", command=_copy_claude_json
                  ).pack(side="left", padx=(0, 6))
        tk.Button(bf, text="💾  분석 결과 저장 (JSON)", font=("Arial", 9),
                  bg=C.BG3, fg=C.FG, relief="flat", bd=0, padx=12, pady=5,
                  cursor="hand2", command=_save_to_file
                  ).pack(side="left")
        tk.Button(bf, text="닫기", font=("Arial", 9),
                  bg=C.BG3, fg=C.FG, relief="flat", bd=0, padx=12, pady=5,
                  cursor="hand2", command=win.destroy
                  ).pack(side="right")

    # ─── GitHub 자동 업로드 ──────────────────────────────────────────────────

    def _on_git_upload(self) -> None:
        """GitHub 업로드 버튼 클릭 → 백그라운드 스레드에서 git add/commit/push 실행"""
        if self._git_uploading:
            return
        self._git_uploading = True
        self._git_btn.config(state="disabled", text="⏳ 업로드 중...", fg=C.YELLOW)
        self._git_status_label.config(text="", fg=C.SUB)
        threading.Thread(target=self._do_git_upload, daemon=True, name="GitUpload").start()

    def _do_git_upload(self) -> None:
        """백그라운드: git add → commit → push 순서로 실행"""
        import subprocess, shutil
        from datetime import datetime
        from zoneinfo import ZoneInfo

        log = logging.getLogger(__name__)
        project_dir = os.path.dirname(os.path.abspath(__file__))
        git_exe = shutil.which("git") or "git"

        def run(args: list[str]) -> tuple[int, str]:
            result = subprocess.run(
                [git_exe] + args,
                cwd=project_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            return result.returncode, (result.stdout + result.stderr).strip()

        try:
            # 1) 변경 파일 확인 (logs/ 와 .env 제외)
            rc, staged = run(["status", "--porcelain"])
            lines = [
                l for l in staged.splitlines()
                if not l[3:].startswith("logs/")
                and not l[3:].startswith(".env")
            ]
            if not lines:
                log.info("[GitHub] 변경 사항 없음 — 업로드 스킵")
                self.after(0, lambda: self._git_upload_done(True, "변경 없음"))
                return

            # 2) git add (logs, .env 제외)
            run(["add", "-A"])
            run(["reset", "--", "logs/", ".env", ".env.local"])

            # staged 여부 재확인
            rc, diff = run(["diff", "--cached", "--name-only"])
            if not diff.strip():
                log.info("[GitHub] 스테이징할 파일 없음")
                self.after(0, lambda: self._git_upload_done(True, "변경 없음"))
                return

            # 3) commit
            now_kst = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST")
            msg = f"[Auto] 프로그램 업데이트 - {now_kst}"
            rc, out = run(["commit", "-m", msg])
            if rc != 0 and "nothing to commit" not in out:
                raise RuntimeError(f"commit 실패: {out}")

            # 4) push
            rc, out = run(["push", "origin", "main"])
            if rc != 0:
                raise RuntimeError(f"push 실패: {out}")

            log.info(f"[GitHub] 업로드 완료: {msg}")
            self.after(0, lambda: self._git_upload_done(True, "업로드 완료 ✓"))

        except Exception as e:
            log.error(f"[GitHub] 업로드 실패: {e}")
            err_msg = str(e)[:60]
            self.after(0, lambda m=err_msg: self._git_upload_done(False, m))

    def _git_upload_done(self, success: bool, message: str) -> None:
        """업로드 완료 후 버튼/레이블 상태 복원"""
        self._git_uploading = False
        self._git_btn.config(state="normal", text="↑ GitHub", fg=C.ACCENT)
        color = C.GREEN if success else C.RED
        self._git_status_label.config(text=message, fg=color)
        # 5초 후 메시지 자동 제거
        self.after(5000, lambda: self._git_status_label.config(text="", fg=C.SUB))

    # ─── 기타 ────────────────────────────────────────────────────────────────

    def _browse_vault(self) -> None:
        path = filedialog.askdirectory(title="옵시디언 볼트 폴더 선택")
        if path:
            self._vault_var.set(path)
            self._obsidian_enabled.set(True)

    def _send_summary_now(self) -> None:
        if self._notif_mgr:
            self._notif_mgr.send_now()
        else:
            messagebox.showinfo("알림", "실행 중일 때만 요약을 전송할 수 있습니다.")

    def _on_close(self) -> None:
        if self._real_running or self._paper_running:
            if not messagebox.askyesno("종료 확인", "실행 중입니다. 종료하시겠습니까?"):
                return
            if self._real_running:
                self._do_stop_real()
            if self._paper_running:
                self._do_stop_paper()
        self.destroy()


if __name__ == "__main__":
    app = TradingApp()
    app.mainloop()
