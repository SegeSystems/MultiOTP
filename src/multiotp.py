"""
MultiOTP - Knight Online icin coklu hesap OTP goruntuleyici
============================================================

NE YAPAR
--------
Knight Online'in resmi OTP sistemi (NTT Games AnyOTP) tek seferde tek hesap
icin tasarlanmis. Coklu hesabin varsa her seferinde:
    1. Account.reg dosyasini import et
    2. SpoofInfo.txt'yi Program Files'a kopyala
    3. AnyOTP.exe'yi ac
    4. Sifreyi gir, OTP kodunu al
yapman gerekiyor — her hesap degisiminde dakikalar.

MultiOTP bunu butona indirir: hesap butonuna basinca registry import + OTP
uretimi tek seferde otomatik olur.


MIMARI (3 KATMAN)
-----------------
+-------------------+     subprocess     +-------------------+
| multiotp.py       | -----------------> | otp_core.exe      |
| (64-bit Python)   |                    | (32-bit C# helper)|
| - Tkinter GUI     |  args:             |   |               |
| - hesap bulma     |  dll, spoof, pwd   |   | P/Invoke       |
| - reg.exe import  |                    |   v               |
| - subprocess      |  stdout: 6 hane    | AnyOTPBiz.dll     |
+-------------------+                    | (NTT Games kripto)|
                                         +-------------------+

Neden iki process? AnyOTPBiz.dll 32-bit. Python 64-bit. 64-bit process
32-bit DLL yukleyemez. otp_core.exe x86 derlenir, koprulu kurar.


HER HESAP IcIN GEREKLI 3 PARCA
-------------------------------
1. otps/<hesap>/SpoofInfo.txt
   - 3 satir: PNP device ID, signature (uint32), tail
   - hardware_id'yi bunlardan turetiyoruz
   - Hesap saglayicidan (USKOHESAP vb.) gelir, biz uretmeyiz

2. otps/<hesap>/OtpInfo.reg
   - HKCU\Software\AnyOTP\VMdata + EnvInfo registry kayitlari
   - VMdata: 48-byte sifrelenmis donanim baglamasi (DLL acar)
   - EnvInfo: ASCII OTP cihaz ID'si + son kullanma tarihi
   - Hesap degisiminde bu dosya import edilir

3. config.json icindeki "passwords" sozlugu
   - Kullanicinin 6-haneli OTP sifresi (kullanicinin AnyOTP'de belirledigi)
   - Bu sifre + donanim kimligi + zaman -> 6-haneli OTP kodu
   - Sifrelemeden duz metin saklanir (lokal kullanim icin pratiklik)


CALISMA AKIsI
-------------
Kullanici hesap butonuna basar:
   _select_account()
     1. config.json'a last_account yaz
     2. Hesabin OtpInfo.reg dosyasini reg.exe ile HKCU'ya import et
     3. Sifre yoksa kullanicidan iste, config.json'a kaydet
     4. _on_fetch() cagir
   _on_fetch()
     5. otp_core.exe <DLL> <SpoofInfo> <pwd> calistir
     6. stdout'tan 6-haneli kodu oku, ekrana bas

Her saniye _tick() calisir, dakika basinda otomatik yenileme tetiklenir.


PACKAGING NOTU
--------------
Source halinde calisirken: SCRIPT_DIR == BUNDLE_DIR (her sey ayni klasorde)
PyInstaller --onefile: BUNDLE_DIR = sys._MEIPASS (temp extract)
Nuitka --onefile:      BUNDLE_DIR = __file__ etrafindaki temp
Her durumda:           SCRIPT_DIR = exe yaninda (otps/, config.json burada)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, simpledialog, ttk

# ============================================================================
# UYGULAMA SABITLERI
# ============================================================================

APP_NAME = "MultiOTP"
APP_SITE_LABEL = "www.segemacro.com"
APP_SITE_URL = "https://www.segemacro.com"
APP_VENDOR = "SegeSystems"


# ============================================================================
# YOL HESAPLAMA (PACKAGING-AWARE)
# ============================================================================
# Source/PyInstaller/Nuitka uc farkli sekilde calisabiliyoruz. Veri yolunu
# (otps/, config.json) ve bundle yolunu (otp_core.exe) ayri ayri dusunmek
# lazim — biri kullanicinin yaninda kalmali, digeri readonly bundle.

def _is_frozen() -> bool:
    """Calisma modu paketlenmis mi (PyInstaller veya Nuitka onefile)?"""
    # PyInstaller ve modern Nuitka her ikisi de sys.frozen=True yapar.
    # Eski Nuitka surumlerinde __compiled__ globalini kontrol ediyoruz (yedek).
    return getattr(sys, "frozen", False) or "__compiled__" in globals()


def _data_dir() -> str:
    """Kullanici verisi yolu (otps/, config.json) — kalici, yazilabilir.

    Frozen modda kullanicinin tikladigi exe yolunu kullaniyoruz (sys.argv[0]).
    Hem PyInstaller hem Nuitka burada ayni davraniyor.

    Source modda __file__ (multiotp.py'nin yolu) yeterli.
    """
    if _is_frozen():
        # sys.argv[0] her iki packagerda da kullanicinin tikladigi exe'yi gosterir
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    return os.path.dirname(os.path.abspath(__file__))


def _bundle_dir() -> str:
    """Bundle icindeki readonly veriler yolu (otp_core.exe burada).

    PyInstaller --onefile: exe acilinca dosyalar sys._MEIPASS temp dizinine
    extract edilir, exit'te silinir.

    Nuitka --onefile: benzer sekilde extract eder ama _MEIPASS yok;
    __file__ etrafindaki temp dizini kullanilir.

    Source modda bundle ile data ayni klasor, fark etmez.
    """
    if _is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return meipass  # PyInstaller
        return os.path.dirname(os.path.abspath(__file__))  # Nuitka
    return os.path.dirname(os.path.abspath(__file__))


# Modul yuklenirken bir kere hesapla, sonradan degismez.
SCRIPT_DIR = _data_dir()
BUNDLE_DIR = _bundle_dir()
APP_CONFIG = os.path.join(SCRIPT_DIR, "config.json")
OTP_CORE = os.path.join(BUNDLE_DIR, "otp_core.exe")
OTPS_DIR = os.path.join(SCRIPT_DIR, "otps")

# AnyOTPBiz.dll'i nereden yukleyecegiz? Onceligi olan yollar:
#   1. Resmi AnyOTPSetup kurulumu (32-bit Program Files) — en yaygin
#   2. 64-bit Program Files (nadir, eski kurulumlar)
#   3. Yan yana fallback (DLL ile beraber bundle ettiyseniz)
# AnyOTPBiz.dll yan dosyalara muhtac (AnyOtpDLL.dll, HookWMIC.dll vb.) —
# Program Files yolu kullanmak en guvenli, butun bagimliliklar orada.
DLL_CANDIDATES = [
    r"C:\Program Files (x86)\AnyOTPSetup\AnyOTPBiz.dll",
    r"C:\Program Files\AnyOTPSetup\AnyOTPBiz.dll",
    os.path.join(SCRIPT_DIR, "AnyOTPBiz.dll"),
]


# ============================================================================
# UI BOYUTLARI VE RENK PALETI
# ============================================================================

SUBPROCESS_TIMEOUT = 15        # otp_core.exe veya reg.exe maksimum bekleme (sn)
BUTTONS_PER_ROW = 3            # hesap butonlari grid'inde satir basina kac buton

# Pencere otomatik buyur/kucurur, hesap sayisina gore.
# total = WINDOW_BASE_HEIGHT + (satir_sayisi * WINDOW_PER_ROW_PX)
WINDOW_WIDTH = 480
WINDOW_BASE_HEIGHT = 360       # baslik+OTP+timer+footer+status sabit kismi (px)
WINDOW_PER_ROW_PX = 40         # bir buton satirinin (padding dahil) yuksekligi
WINDOW_MIN_HEIGHT = 380        # hic hesap yokken bile bu kadar yuksek olsun

# Renk paleti: koyu tema. Tkinter'da varsayilan tema gri ve cirkin, biz
# her widget'a manuel renk veriyoruz. ttk widget'lari icin "clam" temasini
# kullaniyoruz cunku diger temalarda fg/bg ezilebiliyor.
COLOR_BG = "#1e1e2e"           # ana arka plan (koyu lacivert)
COLOR_SURFACE = "#2a2a3e"      # kart/buton arka plani
COLOR_SURFACE_HI = "#34344a"   # hover/active arka plan
COLOR_TEXT = "#e0e0e0"         # ana metin
COLOR_MUTED = "#8a8a9e"        # ikincil metin, footer
COLOR_ACCENT = "#4fc3f7"       # cyan vurgu — aktif hesap, OTP rakamlari
COLOR_SUCCESS = "#66bb6a"      # yesil — basarili islem, kopyalandi feedback
COLOR_WARN = "#ffa726"         # turuncu — eksik veri, uyari
COLOR_DANGER = "#ef5350"       # kirmizi — hata, son saniye geri sayim


# ============================================================================
# YARDIMCI: DLL BUL, CONFIG OKU/YAZ
# ============================================================================

def find_dll() -> str | None:
    """DLL_CANDIDATES'taki ilk var olan yolu donder, hicbiri yoksa None."""
    for path in DLL_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def load_app_config() -> dict:
    """config.json'i oku. Yoksa veya bozuksa bos sozluk donder.

    Sessizce hatayi tolere ediyoruz — ilk acilista config.json olmamasi normal,
    bozulmussa siliyor gibi davranip yeniden olusturmasi mantikli.
    """
    if not os.path.exists(APP_CONFIG):
        return {}
    try:
        with open(APP_CONFIG, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_app_config(data: dict) -> None:
    """config.json'a yaz. Kullanici tarafindan elle okunabilmesi icin pretty-print."""
    try:
        with open(APP_CONFIG, "w", encoding="utf-8") as f:
            # ensure_ascii=False -> Turkce karakterler bozulmasin
            # indent=2 -> JSON insan-okur formatta
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        # Yazma hatasi (ornegin disk dolu, readonly klasor) — uygulamayi
        # cokertmektense sessizce gec; bir sonraki yazmada belki duzelir.
        pass


# ============================================================================
# HESAP KESFI VE LEGACY MIGRATION
# ============================================================================

def discover_accounts() -> list[dict]:
    """otps/ altindaki hesap klasorlerini tara, her biri icin bir sozluk donder.

    Doner: [{"folder_name": ..., "folder_path": ..., "spoof_path": ..., "reg_path": ...}, ...]

    Filtreleme:
    - '_' veya '.' ile baslayan klasorler atlanir (sablon/yedek tutmak icin
      kullanici "_yedek_aprx" gibi adlandirabilir)
    - Dosyalar (klasor olmayan) atlanir
    - Dosyalarin gercekten var olup olmadigini kontrol etmiyoruz —
      _select_account() icinde kontrol ediliyor, kullanici daha iyi feedback alir.
    """
    if not os.path.isdir(OTPS_DIR):
        return []
    accounts = []
    # str.lower key'i ile sirala — case-insensitive (Hobura, hobura ayni grupta)
    for name in sorted(os.listdir(OTPS_DIR), key=str.lower):
        if name.startswith(("_", ".")):
            continue  # _example, _template gibi sablon/yardim klasorleri
        folder = os.path.join(OTPS_DIR, name)
        if not os.path.isdir(folder):
            continue
        accounts.append({
            "folder_name": name,
            "folder_path": folder,
            "spoof_path": os.path.join(folder, "SpoofInfo.txt"),
            "reg_path": os.path.join(folder, "OtpInfo.reg"),
        })
    return accounts


def migrate_legacy_account_json(app_config: dict, accounts: list[dict]) -> bool:
    """Eski versiyon her hesap klasorunde account.json tutuyordu (sifre dahil).
    Yeni versiyonda sifreler tek bir config.json icinde "passwords" sozlugunde.

    Bu fonksiyon eski account.json dosyalarindaki sifreyi config.json'a tasir.
    config.json'da sifre zaten varsa eskiyi gormezden gelir (overwrite yok).

    Doner: degisiklik yapildi mi (caller save_app_config cagirsin diye).
    """
    changed = False
    passwords = app_config.setdefault("passwords", {})
    for acct in accounts:
        meta = os.path.join(acct["folder_path"], "account.json")
        if not os.path.exists(meta):
            continue
        try:
            with open(meta, "r", encoding="utf-8") as f:
                legacy = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        pwd = (legacy.get("otp_password") or "").strip()
        if pwd and not passwords.get(acct["folder_name"]):
            passwords[acct["folder_name"]] = pwd
            changed = True
    return changed


# ============================================================================
# SUBPROCESS YARDIMCILARI (Windows'ta konsol gostermemek icin)
# ============================================================================

def _hide_window_startupinfo():
    """Subprocess cagirirken kara CMD penceresinin gozukmemesi icin STARTUPINFO.

    Windows'ta GUI uygulamasindan reg.exe veya otp_core.exe gibi konsol
    process'i spawn ettiginde, kullanici 1 saniyelik kara pencere goruyor.
    STARTF_USESHOWWINDOW + SW_HIDE bu pencereyi tamamen gizler.

    Linux/Mac'te bu kavram yok, None donderiyoruz (subprocess gormezden gelir).
    """
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return si


def import_registry(reg_path: str) -> tuple[bool, str]:
    """Verilen .reg dosyasini Windows registry'sine import eder (HKCU, admin yok).

    Her hesap secildiginde cagrilmali — DLL VMdata/EnvInfo'yu HKCU'dan okur,
    yani registry'deki aktif degerler her zaman secili hesabin verisi olmali.

    `reg.exe import` Windows'ta hep var, ekstra kurulum gerekmez.
    HKCU yazimi admin yetkisi istemez.

    Doner: (basarili_mi, hata_mesaji_eger_basarisizsa).
    """
    if not os.path.exists(reg_path):
        return False, f"Reg dosyasi yok: {reg_path}"
    try:
        result = subprocess.run(
            ["reg.exe", "import", reg_path],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
            startupinfo=_hide_window_startupinfo(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"Reg import hatasi: {exc}"
    if result.returncode != 0:
        # reg.exe genelde stderr'e basit hata mesaji yazar
        return False, (result.stderr or result.stdout or "reg import basarisiz").strip()
    return True, ""


def generate_otp(dll_path: str, spoof_path: str, password: str) -> tuple[bool, str]:
    """otp_core.exe'yi cagirip 6-haneli OTP kodunu uretir.

    otp_core.exe protokolu (otp_core.cs'de tanimli):
        argv: <dll_path> <spoof_path> <password>
        stdout (basari): "123456\\n"
        stderr (hata):   aciklayici mesaj
        exit code:       0 basari, 1-10 farkli hata sebepleri

    Coklu hesap zinciri:
        1. import_registry(reg_path) — DLL'in okuyacagi VMdata/EnvInfo guncelle
        2. generate_otp(dll, spoof, pwd) — burasi
    Bu sira onemli — registry yanlis hesabin VMdata'sini iceriyorsa OTP yanlis cikar.

    Doner: (basarili_mi, OTP_kodu_veya_hata_mesaji).
    """
    if not os.path.exists(OTP_CORE):
        return False, f"otp_core.exe bulunamadi: {OTP_CORE}"
    try:
        result = subprocess.run(
            [OTP_CORE, dll_path, spoof_path, password],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
            startupinfo=_hide_window_startupinfo(),
        )
    except subprocess.TimeoutExpired:
        return False, "Zaman asimi (15sn)"
    except OSError as exc:
        # exe yok, baslatilamadi vb.
        return False, f"Calistirilamadi: {exc}"
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    # Saglik kontrolu: OTP tam 6 hane decimal olmali, '000000' aslinda hata
    # (otp_core "geceriz OTP uretilemedi" diye stderr'e atip bunu da basabilir).
    if out.isdigit() and len(out) == 6 and out != "000000":
        return True, out
    return False, err or out or "bos cikti"


# ============================================================================
# ANA UYGULAMA SINIFI
# ============================================================================

class OtpApp:
    """Tum UI durumunu ve davranisini tutan tek sinif. Tkinter root'una baglanir.

    Kurulus:
        root = tk.Tk()
        OtpApp(root)
        root.mainloop()

    State:
        accounts:        otps/ taramasinin sonucu (her birinde folder_name, paths)
        account_buttons: folder_name -> tk.Button mapping (active highlighting icin)
        current:         secili hesap dict'i (None ise hicbiri secilmemis)
        current_otp:     en son uretilen 6-haneli kod (kopyalanacak deger)
        last_fetch_minute: en son OTP cekildiginde dakika — auto-refresh kontrolu
        auto_refresh_var:  Tkinter BooleanVar (checkbox state, config.json'a kalici)
    """

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.app_config = load_app_config()
        self.app_config.setdefault("passwords", {})
        self.accounts: list[dict] = []
        self.account_buttons: dict[str, tk.Button] = {}
        self.current: dict | None = None
        self.current_otp = ""
        self.last_fetch_minute = -1  # ilk acilis: hicbir dakika "son" kabul edilmesin
        self.auto_refresh_var = tk.BooleanVar(
            value=bool(self.app_config.get("auto_refresh", True))
        )
        self.dll_path = find_dll()

        # Sira onemli: UI ilk olusur, sonra preflight (eksik dosya warning),
        # sonra hesaplari yukle (varsa son secileni acar), en son tick baslat.
        self._build_ui()
        self._preflight()
        self._reload_accounts()
        self._tick()

    # ----------------------------------------------------------------
    # UI INSAATI
    # ----------------------------------------------------------------

    def _build_ui(self) -> None:
        """Pencere + tum widget'lari olustur. Sadece bir kere cagrilir.

        Layout (yukaridan asagi pack ile):
            [Baslik MultiOTP]
            [Hesap butonlari grid'i]    <- _rebuild_account_buttons doldurur
            [Sifre / Klasor / Yenile]
            [OTP rakamlari + butonlar]
            [Progressbar + dakika sayaci]
            [Otomatik yenile checkbox]
            [Footer: powered by SegeSystems]   side="bottom"
            [Status mesaji]                    side="bottom"
        """
        self.root.title(APP_NAME)
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_MIN_HEIGHT}")
        self.root.resizable(False, False)
        self.root.configure(bg=COLOR_BG)
        # Pencereyi ekranin ortasinda ac (Tk dahili komut)
        self.root.eval("tk::PlaceWindow . center")

        # ttk widget'lari (Progressbar) icin stil. "clam" temasi rengi
        # ezmek icin gerekli — varsayilan Windows temasinda fg/bg ignore edilir.
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass  # eski Tk surumlerinde "clam" yoksa varsayilan kalsin
        style.configure(
            "TProgressbar",
            troughcolor=COLOR_SURFACE, background=COLOR_ACCENT,
            bordercolor=COLOR_SURFACE, lightcolor=COLOR_ACCENT, darkcolor=COLOR_ACCENT,
        )

        # Baslik
        tk.Label(
            self.root, text=APP_NAME,
            font=("Segoe UI", 14, "bold"),
            bg=COLOR_BG, fg=COLOR_ACCENT,
        ).pack(pady=(14, 6))

        # Hesap butonlari icin bos frame — _rebuild_account_buttons icindeki
        # grid'le sonradan dolduruluyor. Her _reload_accounts cagrisinda
        # icindeki widget'lar destroy edilip yeniden olusturuluyor.
        self.accounts_frame = tk.Frame(self.root, bg=COLOR_BG)
        self.accounts_frame.pack(fill="x", padx=20, pady=(0, 6))

        # Arac butonlari satiri: aktif hesabi etkileyen aksiyonlar
        tools = tk.Frame(self.root, bg=COLOR_BG)
        tools.pack(fill="x", padx=20, pady=(0, 8))
        self._tool_btn(tools, "Sifre", self._edit_password).pack(side="left", padx=(0, 4))
        self._tool_btn(tools, "Klasor", self._open_folder).pack(side="left", padx=4)
        self._tool_btn(tools, "Yenile", self._reload_accounts).pack(side="left", padx=4)

        # OTP gosterim alani — koyu surface kart goruntusunde
        otp_frame = tk.Frame(self.root, bg=COLOR_SURFACE)
        otp_frame.pack(fill="x", padx=20, pady=6, ipady=14)
        # StringVar -> Label.config(text=...) yerine .set() ile guncellenebilir
        self.otp_var = tk.StringVar(value="------")
        self.otp_label = tk.Label(
            otp_frame, textvariable=self.otp_var,
            font=("Consolas", 38, "bold"),
            bg=COLOR_SURFACE, fg=COLOR_ACCENT, cursor="hand2",
        )
        self.otp_label.pack(pady=(2, 4))
        # Etiketin kendisine tiklamak da kopyalama yapsin (UX kolayligi)
        self.otp_label.bind("<Button-1>", lambda _e: self._copy_otp())

        # OTP butonlari (yenile + kopyala)
        btn_row = tk.Frame(otp_frame, bg=COLOR_SURFACE)
        btn_row.pack()
        self.fetch_btn = self._colored_btn(btn_row, "OTP Cek", self._on_fetch, COLOR_SUCCESS)
        self.fetch_btn.pack(side="left", padx=4)
        self.copy_btn = self._colored_btn(btn_row, "Panoya Kopyala", self._copy_otp, COLOR_ACCENT)
        self.copy_btn.pack(side="left", padx=4)
        # _flash_copied'in yarida olup olmadigini takip etmek icin after ID
        # (timer iptal edebilmek icin)
        self._copy_flash_after_id: str | None = None

        # Geri sayim — her saniye _tick guncelliyor
        timer_frame = tk.Frame(self.root, bg=COLOR_BG)
        timer_frame.pack(fill="x", padx=20, pady=(6, 2))
        # maximum=60 cunku OTP her 60 saniyede yenileniyor (tam dakika basinda)
        self.time_progress = ttk.Progressbar(
            timer_frame, orient="horizontal", mode="determinate", maximum=60,
        )
        self.time_progress.pack(fill="x", pady=(0, 2))
        self.time_label = tk.Label(
            timer_frame, text="--", font=("Segoe UI", 9),
            bg=COLOR_BG, fg=COLOR_MUTED, anchor="e",
        )
        self.time_label.pack(fill="x")

        # Otomatik yenile secenek kutusu
        tk.Checkbutton(
            self.root, text="Otomatik yenile (her dakika)",
            variable=self.auto_refresh_var, command=self._on_toggle_auto,
            bg=COLOR_BG, fg=COLOR_TEXT, selectcolor=COLOR_SURFACE,
            activebackground=COLOR_BG, activeforeground=COLOR_TEXT,
            font=("Segoe UI", 9), cursor="hand2",
        ).pack(anchor="w", padx=20)

        # Footer: side="bottom" -> en alta yapisir, status_label'in da altinda
        # Tkinter'in side="bottom" davranisi: ilk pack edilen en alttadir, sonraki
        # ondan yukariya yerlesir. Yani footer'i status_label'dan ONCE pack ediyoruz.
        footer = tk.Frame(self.root, bg=COLOR_BG)
        footer.pack(side="bottom", fill="x", pady=(2, 6))
        powered = tk.Label(
            footer,
            text=f"Powered by {APP_VENDOR}  -  {APP_SITE_LABEL}",
            font=("Segoe UI", 8),
            bg=COLOR_BG, fg=COLOR_MUTED,
            cursor="hand2",
        )
        powered.pack()
        # Tikla -> tarayicida sitemizi ac
        powered.bind("<Button-1>", lambda _e: webbrowser.open(APP_SITE_URL))
        # Hover efekti: muted -> accent, leave: tersi
        powered.bind("<Enter>", lambda _e: powered.config(fg=COLOR_ACCENT))
        powered.bind("<Leave>", lambda _e: powered.config(fg=COLOR_MUTED))

        # Status mesaji — footer'dan sonra pack ediyoruz, yani footer'in UZERINDE goruncek
        self.status_var = tk.StringVar(value="Hazir")
        self.status_label = tk.Label(
            self.root, textvariable=self.status_var,
            font=("Segoe UI", 9), bg=COLOR_BG, fg=COLOR_MUTED,
            anchor="w", padx=20,
        )
        self.status_label.pack(side="bottom", fill="x", pady=(0, 4))

    # Buton fabrikalari — DRY (kendini tekrar etme) icin

    def _tool_btn(self, parent, text, command):
        """Sifre/Klasor/Yenile gibi nitral aksiyonlar icin koyu, ince buton."""
        return tk.Button(
            parent, text=text, command=command,
            bg=COLOR_SURFACE_HI, fg=COLOR_TEXT,
            activebackground=COLOR_SURFACE_HI, activeforeground="white",
            font=("Segoe UI", 9, "bold"), relief="flat",
            cursor="hand2", padx=14, pady=4, bd=0,
        )

    def _colored_btn(self, parent, text, command, bg):
        """OTP Cek (yesil) / Panoya Kopyala (cyan) gibi vurgulu CTA butonlari."""
        return tk.Button(
            parent, text=text, command=command,
            bg=bg, fg="white",
            activebackground=bg, activeforeground="white",
            font=("Segoe UI", 9, "bold"), relief="flat",
            cursor="hand2", padx=12, pady=4, bd=0,
        )

    # ----------------------------------------------------------------
    # HESAP BUTONLARI (DINAMIK GRID)
    # ----------------------------------------------------------------

    def _rebuild_account_buttons(self) -> None:
        """accounts_frame icini sifirdan yeniden olustur.

        Her _reload_accounts'ta cagrilir — kullanici klasor ekleyip "Yenile"ye
        bastiginda yeni butonlar belirir, eskileri silinir.

        BUTTONS_PER_ROW=3 -> 3 sutunlu grid; satir sayisi hesap sayisina gore.
        Bos durumda placeholder mesaj gosterir.
        """
        # Onceki butonlari temizle (eger _reload zaten cagrilmissa)
        for w in self.accounts_frame.winfo_children():
            w.destroy()
        self.account_buttons = {}

        if not self.accounts:
            tk.Label(
                self.accounts_frame,
                text="otps/ altinda hesap klasoru yok",
                bg=COLOR_BG, fg=COLOR_MUTED, font=("Segoe UI", 9),
            ).pack(pady=10)
            self._resize_window()
            return

        for i, acct in enumerate(self.accounts):
            r, c = divmod(i, BUTTONS_PER_ROW)
            # NOT: lambda icindeki `a=acct` zorunlu — closure'da late binding sorunu
            # var, `a=acct` ile o anki acct degeri yakalaniyor (yoksa hep son acct kullanilir).
            btn = tk.Button(
                self.accounts_frame,
                text=acct["folder_name"],
                command=lambda a=acct: self._select_account(a),
                bg=COLOR_SURFACE, fg=COLOR_TEXT,
                activebackground=COLOR_SURFACE_HI, activeforeground="white",
                font=("Segoe UI", 10, "bold"), relief="flat",
                cursor="hand2", pady=8, bd=0,
            )
            btn.grid(row=r, column=c, padx=3, pady=3, sticky="ew")
            self.account_buttons[acct["folder_name"]] = btn

        # Tum sutunlar esit genislikte uzasin (sticky="ew" ile birlikte)
        for c in range(BUTTONS_PER_ROW):
            self.accounts_frame.columnconfigure(c, weight=1)

        self._resize_window()

    def _resize_window(self) -> None:
        """Hesap sayisina gore pencere yuksekligini dinamik olarak ayarla.

        ceil(n/BUTTONS_PER_ROW) -> satir sayisi (Python integer math hilesi).
        Cok az hesap -> WINDOW_MIN_HEIGHT'a clamp ediliyor (cok sikistirma).
        """
        n = len(self.accounts)
        rows = max(1, (n + BUTTONS_PER_ROW - 1) // BUTTONS_PER_ROW)
        height = max(WINDOW_MIN_HEIGHT, WINDOW_BASE_HEIGHT + rows * WINDOW_PER_ROW_PX)
        self.root.geometry(f"{WINDOW_WIDTH}x{height}")

    def _highlight_active(self) -> None:
        """Aktif hesabin butonunu accent renge boya, digerlerini surface renge cevir.

        Her _select_account sonrasi cagrilmali. Bu fonksiyon tum butonlari
        guncelliyor, sadece eski-yeni aktif olan ikisi degil — basitlik icin.
        """
        active = self.current["folder_name"] if self.current else None
        for name, btn in self.account_buttons.items():
            if name == active:
                btn.config(bg=COLOR_ACCENT, fg="white")
            else:
                btn.config(bg=COLOR_SURFACE, fg=COLOR_TEXT)

    # ----------------------------------------------------------------
    # ACILIS KONTROLLERI VE HESAP YUKLEME
    # ----------------------------------------------------------------

    def _preflight(self) -> None:
        """Acilista kritik dosyalarin var oldugunu kontrol et, eksikse status'a yaz.

        Bu fonksiyon hata firlatmiyor — sadece kullaniciyi bilgilendiriyor.
        Eksik bir sey varsa OTP uretmeye calistiginda zaten hata cikacak.
        """
        missing = []
        if not os.path.exists(OTP_CORE):
            missing.append("otp_core.exe")
        if not self.dll_path:
            missing.append("AnyOTPBiz.dll")
        if not os.path.isdir(OTPS_DIR):
            missing.append("otps/ klasoru")
        if missing:
            self._status(f"Eksik: {', '.join(missing)}", COLOR_DANGER)

    def _reload_accounts(self) -> None:
        """otps/'ye yeniden bak, butonlari yeniden olustur, son secileni ac.

        "Yenile" butonu bunu cagiriyor. Ayrica ilk acilista bir kere __init__ icinde.

        Akis:
            1. discover_accounts() -> klasorleri tara
            2. Eski account.json sifrelerini config.json'a tasi (varsa)
            3. UI'da butonlari yeniden olustur
            4. Son secili hesabi ac (yoksa ilkini)
        """
        self.accounts = discover_accounts()
        # Legacy goc — eski versiyondan gelenler icin
        if migrate_legacy_account_json(self.app_config, self.accounts):
            save_app_config(self.app_config)
        self._rebuild_account_buttons()

        if not self.accounts:
            self.current = None
            self.otp_var.set("------")
            self._status("otps/ altinda hesap bulunamadi", COLOR_WARN)
            return

        # last_account config'de yoksa veya o klasor silinmisse ilk hesaba dus
        last = self.app_config.get("last_account", "")
        target = next(
            (a for a in self.accounts if a["folder_name"] == last),
            self.accounts[0],
        )
        self._select_account(target)

    # ----------------------------------------------------------------
    # SIFRE STORAGE (config.json icindeki "passwords" sozlugu)
    # ----------------------------------------------------------------

    def _pwd_for(self, folder_name: str) -> str:
        """folder_name icin kayitli sifreyi don, yoksa bos string."""
        return (self.app_config.get("passwords") or {}).get(folder_name, "")

    def _set_pwd(self, folder_name: str, value: str) -> None:
        """folder_name icin sifreyi kaydet ve config.json'a yaz."""
        self.app_config.setdefault("passwords", {})[folder_name] = value
        save_app_config(self.app_config)

    # ----------------------------------------------------------------
    # HESAP SECME, OTP CEKME
    # ----------------------------------------------------------------

    def _select_account(self, acct: dict) -> None:
        """Hesabi aktif yap: registry import + (varsa) OTP cek.

        Akis (muhtelif erken donus noktalari ile):
            1. State guncelle, button highlight
            2. OtpInfo.reg / SpoofInfo.txt var mi kontrol et
            3. reg.exe import — registry'yi bu hesabin VMdata'sina cevir
            4. Sifre var mi? Yoksa kullanicidan iste
            5. _on_fetch() — OTP'yi uret
        """
        self.current = acct
        self.current_otp = ""
        self.otp_var.set("------")  # eski OTP'yi sil — yeni hesabin OTP'si daha uretilmedi
        self.app_config["last_account"] = acct["folder_name"]
        save_app_config(self.app_config)
        self._highlight_active()

        # Dosyalar yerinde mi
        if not os.path.exists(acct["reg_path"]):
            self._status(f"{acct['folder_name']}: OtpInfo.reg yok", COLOR_DANGER)
            return
        if not os.path.exists(acct["spoof_path"]):
            self._status(f"{acct['folder_name']}: SpoofInfo.txt yok", COLOR_DANGER)
            return

        # Registry'yi guncelle — DLL aktif degerleri okuyacak
        ok, err = import_registry(acct["reg_path"])
        if not ok:
            self._status(f"Reg yukleme hatasi: {err[:60]}", COLOR_DANGER)
            return

        # Sifre yoksa kullanicidan iste, varsa direkt OTP uret
        if not self._pwd_for(acct["folder_name"]):
            self._status(f"{acct['folder_name']}: sifre gerekli", COLOR_WARN)
            if self._prompt_password(acct, first_time=True):
                self._on_fetch()
        else:
            self._on_fetch()

    def _prompt_password(self, acct: dict, first_time: bool = False) -> bool:
        """Modal dialog'la sifre iste, dogrula, kaydet.

        first_time=True: "OTP Sifresi" basligi — ilk defa sorulduguna dair ipucu
        first_time=False: "Sifre Degistir" — kullanici Sifre butonuna basti

        Validasyon: bos olmamali, sadece harf-rakam icermeli (AnyOTP'nin kurali).

        Doner: True = sifre kaydedildi, caller OTP cekebilir
               False = kullanici cancel basti veya validasyon basarisiz
        """
        current = self._pwd_for(acct["folder_name"])
        title = "OTP Sifresi" if first_time else "Sifre Degistir"
        prompt = f"'{acct['folder_name']}' hesabinin 6 haneli OTP sifresi:"
        # parent=self.root -> dialog ana pencerenin uzerinde acilir, dogru focus
        value = simpledialog.askstring(title, prompt, initialvalue=current, parent=self.root)
        if value is None:
            # Kullanici cancel basti veya X'le kapatti
            return False
        value = value.strip()
        if not value:
            self._status("Bos sifre kaydedilmedi", COLOR_WARN)
            return False
        if not value.isalnum():
            messagebox.showwarning("Uyari", "Sifre sadece harf ve rakam icermeli.")
            return False
        self._set_pwd(acct["folder_name"], value)
        self._status(f"{acct['folder_name']}: sifre kaydedildi", COLOR_SUCCESS)
        return True

    def _edit_password(self) -> None:
        """Sifre butonu callback'i — secili hesabin sifresini degistir."""
        if not self.current:
            self._status("Once bir hesap secin", COLOR_WARN)
            return
        if self._prompt_password(self.current, first_time=False):
            # Sifre degisti, hemen yeni sifreyle OTP yenile
            self._on_fetch()

    def _on_fetch(self) -> None:
        """Aktif hesap icin OTP uret. UI'yi gunceller, panoya yazmaz.

        OTP Cek butonu, _select_account ve _tick (auto-refresh) bunu cagiriyor.

        Onemli: panoya kopyalama YAPMAZ — bu kasitli, kullaniciyi sasirtmamak
        icin. Sadece kullanici Panoya Kopyala'ya bastiginda kopyalanir.
        """
        if not self.current:
            messagebox.showwarning("Uyari", "Once bir hesap secin.")
            return
        if not self.dll_path:
            messagebox.showerror("Hata", "AnyOTPBiz.dll bulunamadi. AnyOTPSetup kurulu mu?")
            return
        if not os.path.exists(self.current["spoof_path"]):
            messagebox.showerror("Hata", f"SpoofInfo yok: {self.current['spoof_path']}")
            return

        password = self._pwd_for(self.current["folder_name"])
        if not password:
            # Auto-refresh sirasinda sifre silinmisse (cok nadir) buradan tekrar iste
            if not self._prompt_password(self.current, first_time=True):
                return
            password = self._pwd_for(self.current["folder_name"])

        # UI feedback: dugmeyi devre disi birak, status'a "cekiliyor" yaz
        self._status("OTP cekiliyor...", COLOR_WARN)
        self.fetch_btn.config(state="disabled")
        self.root.update_idletasks()  # UI'yi hemen tazele (subprocess oncesi)

        ok, value = generate_otp(self.dll_path, self.current["spoof_path"], password)

        self.fetch_btn.config(state="normal")

        if ok:
            self.current_otp = value
            self.otp_var.set(value)
            # Auto-refresh icin son cekim dakikasini kaydet — ayni dakikada tekrar cekmesin
            self.last_fetch_minute = int(time.strftime("%M"))
            self._status(
                f"[{self.current['folder_name']}] OTP hazir",
                COLOR_SUCCESS,
            )
        else:
            self.current_otp = ""
            self.otp_var.set("------")
            self._status(f"Basarisiz: {value[:70]}", COLOR_DANGER)
            messagebox.showerror("OTP Hatasi", f"OTP alinamadi.\n\nDetay:\n{value}")

    # ----------------------------------------------------------------
    # KOPYALAMA (panoya yazma + gorsel feedback)
    # ----------------------------------------------------------------

    def _copy_otp(self) -> None:
        """OTP'yi panoya yaz, "KOPYALANDI" feedback'ini tetikle."""
        if not self.current_otp:
            self._status("Kopyalanacak OTP yok", COLOR_WARN)
            return
        # Panoyu temizleyip OTP'yi yaz — tkinter'in dahili clipboard API'si.
        # Panonun kalmasi icin app calisiyor olmali ama Tk genelde sistem
        # panosunu da senkron eder.
        self.root.clipboard_clear()
        self.root.clipboard_append(self.current_otp)
        self._status("OTP panoya kopyalandi", COLOR_SUCCESS)
        self._flash_copied()

    def _flash_copied(self) -> None:
        """1.5 saniyelik gorsel feedback: buton yesile dondu, bell sesi.

        Pesi pesine cok hizli kopyalanirsa onceki timer'i iptal ediyoruz
        (after_cancel) — yoksa rastgele zamanda revert tetiklenebilir.
        """
        # Onceki revert henuz olmadiysa iptal et
        if self._copy_flash_after_id is not None:
            try:
                self.root.after_cancel(self._copy_flash_after_id)
            except tk.TclError:
                # ID gecersiz olabilir, gormezden gel
                pass
            self._copy_flash_after_id = None

        # Anlik gorsel cevap
        self.copy_btn.config(text="KOPYALANDI", bg=COLOR_SUCCESS)
        self.otp_label.config(fg=COLOR_SUCCESS)
        try:
            self.root.bell()  # OS standart "ding" sesi — bilgisayar sessiz olabilir
        except tk.TclError:
            pass

        def _revert():
            """1500ms sonra eski hale don."""
            self.copy_btn.config(text="Panoya Kopyala", bg=COLOR_ACCENT)
            self.otp_label.config(fg=COLOR_ACCENT)
            self._copy_flash_after_id = None

        self._copy_flash_after_id = self.root.after(1500, _revert)

    # ----------------------------------------------------------------
    # YARDIMCI AKSIYONLAR
    # ----------------------------------------------------------------

    def _open_folder(self) -> None:
        """Aktif hesabin klasorunu Explorer'da ac (yoksa otps/ klasorunu)."""
        target = self.current["folder_path"] if self.current else OTPS_DIR
        if not os.path.isdir(target):
            self._status("Klasor yok", COLOR_WARN)
            return
        try:
            # os.startfile -> Windows'ta "default action with this folder/file"
            # Klasor icin bu Explorer'da acmak demek.
            os.startfile(target)
        except OSError as exc:
            self._status(f"Klasor acilamadi: {exc}", COLOR_DANGER)

    def _on_toggle_auto(self) -> None:
        """Otomatik yenile checkbox callback'i — durumu config.json'a kalici yaz."""
        self.app_config["auto_refresh"] = self.auto_refresh_var.get()
        save_app_config(self.app_config)

    def _status(self, text: str, color: str = COLOR_MUTED) -> None:
        """Status bar mesajini ve rengini guncelle (tek satir)."""
        self.status_var.set(text)
        self.status_label.config(fg=color)

    # ----------------------------------------------------------------
    # ANA TICK LOOP (her saniye)
    # ----------------------------------------------------------------

    def _tick(self) -> None:
        """Saniyede bir cagrilan ana zamanlayici callback'i.

        Iki gorevi var:
        1. Geri sayim ve progressbar guncelle (gorsel)
        2. Dakika basinda otomatik OTP yenileme (auto_refresh acikken)

        AnyOTP OTP'leri her dakika basinda (saniye 0'da) yenilenir.
        Ekrandaki kod 1 dakika gecerli, sonra 1 dakika daha yeni kod gecerli.
        Otomatik yenileme icin: yeni dakika basladiginda (last_fetch_minute
        guncellenenden farkli) otp cek.
        """
        sec = int(time.strftime("%S"))
        minute = int(time.strftime("%M"))
        remaining = 60 - sec  # bu dakikanin kalan saniyesi

        # Progressbar: 60'tan 0'a sayar (kalan zaman gostergesi)
        self.time_progress["value"] = remaining

        # Etiket: "Kalan: 42 sn | hobura75"
        suffix = f" | {self.current['folder_name']}" if self.current else ""
        self.time_label.config(
            text=f"Kalan: {remaining:02d} sn{suffix}",
            # Son 5 saniyede kirmizi (dikkat!) - kod birazdan degisecek
            fg=COLOR_DANGER if remaining <= 5 else COLOR_MUTED,
        )

        # Otomatik yenileme tetikleyicisi
        # Tum kosullar saglanmali:
        #   - auto_refresh acik
        #   - zaten bir OTP gosteriyoruz (yenilenecek bir sey var)
        #   - bir hesap secili
        #   - dakika gercekten degisti (son cekimden bu yana)
        #   - dakikanin ilk saniyesi degil (DLL bazen kod degisiminde gec kaliyor —
        #     sec >= 1 ile 1-saniye guvenlik payini birakiyoruz)
        #   - su an zaten OTP cekmiyoruz (fetch_btn devre disi degil)
        if (
            self.auto_refresh_var.get()
            and self.current_otp
            and self.current
            and minute != self.last_fetch_minute
            and sec >= 1
            and str(self.fetch_btn["state"]) == "normal"
        ):
            self._on_fetch()

        # Bir saniye sonra kendini tekrar cagir — Tkinter'da event loop entegre cron
        self.root.after(1000, self._tick)


# ============================================================================
# GIRIS NOKTASI
# ============================================================================

def main() -> None:
    root = tk.Tk()
    OtpApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
