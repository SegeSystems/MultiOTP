// =============================================================================
// otp_core.cs — AnyOTPBiz.dll kupru programi (kopya modu)
// =============================================================================
//
// NE ISE YARAR
// ------------
// Knight Online'in OTP sistemi (NTT Games AnyOTP) sifrelenmis 32-bit bir DLL
// (AnyOTPBiz.dll) icindeki bir fonksiyona dayaniyor. Bu DLL hardware_id +
// password girdileri ile 6-haneli OTP kodu uretiyor.
//
// Sorun: Python (multiotp.py) 64-bit, AnyOTPBiz.dll 32-bit. 64-bit process
// 32-bit DLL yukleyemez (Windows'un "no mixing" kurali). Bu yuzden bu kucuk
// koprü programa ihtiyacimiz var:
//   - x86 (32-bit) derlenir
//   - DLL'i yukler, fonksiyonu cagirir
//   - Sonucu stdout'a yazar
//   - multiotp.py subprocess ile cagirir
//
//
// CALISMA AKISI
// -------------
// 1. argv'den dll, spoof, password al
// 2. SpoofInfo.txt oku (3 satir): PNP device ID, signature, tail
// 3. Ondan hardware_id stringini olustur (algoritma asagida)
// 4. AnyOTPBiz.dll'i LoadLibraryExW ile yukle
// 5. DLL'in icinde +25383 offsetindeki fonksiyonu (`GenerateOTP`) bul
// 6. Cagir: GenerateOTP(0, hardware_id, password, out codeAddress)
// 7. codeAddress'i memory'den oku (UTF-16, 6 karakter)
// 8. stdout'a yaz, exit
//
//
// DERLEME
// -------
//   csc /platform:x86 /optimize /out:otp_core.exe otp_core.cs
//
// /platform:x86 SART — varsayilan AnyCPU kullanirsan DLL load fail eder.
//
//
// BAGIMLILIK NOTU
// ---------------
// AnyOTPBiz.dll yan dosyalara muhtac (AnyOtpDLL.dll, AnyOtpDLLwDH.dll,
// HookWMIC.dll). LoadLibraryExW'a LOAD_WITH_ALTERED_SEARCH_PATH flag'i
// veriyoruz ki Windows DLL'in kendi klasorunden bagimliliklari arasin —
// yani AnyOTPSetup'in "C:\Program Files (x86)\AnyOTPSetup\" yolunu pass
// etmek yeterli, yan dosyalar otomatik bulunur.
//
//
// HATA KODLARI (exit code)
// ------------------------
// 0  : basari
// 1  : argument eksik
// 2  : DLL bulunamadi
// 3  : SpoofInfo.txt bulunamadi
// 4  : SpoofInfo format hatali (2 satirdan az)
// 5  : Signature parse edilemedi
// 6  : PNP ID 16 karakterden kisa
// 7  : LoadLibraryExW basarisiz (genelde dependency eksik)
// 8  : GenerateOTP NULL pointer dondu
// 9  : ReadProcessMemory basarisiz
// 10 : OTP "000000" geldi (gecersiz hardware/password kombinasyonu)
// =============================================================================

using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

class OtpCore {
    // -------------------------------------------------------------------------
    // SABITLER
    // -------------------------------------------------------------------------

    // LoadLibraryExW flag'leri (winnt.h):
    //   LOAD_IGNORE_CODE_AUTHZ_LEVEL (0x10): Authenticode imza kontrolunu atlat.
    //     AnyOTPBiz.dll imzasiz olabiliyor — bazi sistemlerde bu olmadan reddediliyor.
    //   LOAD_WITH_ALTERED_SEARCH_PATH (0x08): DLL'in kendi klasorunu de arama
    //     yoluna ekle — bagimliliklar (HookWMIC.dll vb.) ayni klasordeyse bulunsun.
    const uint LOAD_IGNORE_CODE_AUTHZ_LEVEL   = 0x00000010;
    const uint LOAD_WITH_ALTERED_SEARCH_PATH  = 0x00000008;
    const uint LOAD_FLAGS = LOAD_IGNORE_CODE_AUTHZ_LEVEL | LOAD_WITH_ALTERED_SEARCH_PATH;

    // GenerateOTP fonksiyonu DLL base'inden bu byte kadar ileride.
    // Reverse engineering ile bulundu (otologin.py'den miras) — DLL surumlerine
    // gore degisebilir, ama AnyOTPBiz.dll yillardir guncellenmedi.
    const int FUNCTION_OFFSET = 25383;

    // OTP buffer: 6 karakter UTF-16LE. Her karakter 2 byte = 12 byte.
    // Plus null terminator olabilir, ama 12 byte ile sinirli okuyup TrimEnd('\0').
    const int OTP_BUFFER_BYTES = 12;

    // -------------------------------------------------------------------------
    // P/INVOKE BILDIRIMLERI (Windows API)
    // -------------------------------------------------------------------------

    /// <summary>DLL yukle (extended). Wide string version, Unicode dosya yolu desteklemek icin.</summary>
    [DllImport("kernel32", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern IntPtr LoadLibraryExW(string lpFileName, IntPtr hFile, uint dwFlags);

    /// <summary>DLL'i serbest birak (RAII zorunlulugu).</summary>
    [DllImport("kernel32", SetLastError = true)]
    static extern bool FreeLibrary(IntPtr hModule);

    /// <summary>Bir process'in memory'sinden okuma yap.
    /// Buradaki kullanim: GetCurrentProcess() ile kendi memory'mizden okuyoruz —
    /// klasik teknik degil, normalde Marshal.Copy ile yapilir. Ama orijinal Python
    /// kodu (otologin.py) bu API'yi kullaniyordu, biz davranisi birebir koruduk.</summary>
    [DllImport("kernel32", SetLastError = true)]
    static extern bool ReadProcessMemory(
        IntPtr hProcess, IntPtr lpBaseAddress, IntPtr lpBuffer,
        int dwSize, out int lpNumberOfBytesRead);

    [DllImport("kernel32")]
    static extern IntPtr GetCurrentProcess();

    /// <summary>GenerateOTP'nin signature'i.
    /// stdcall calling convention (Windows DLL fonksiyonlarinin standart conv'u).
    /// Unicode string'leri LPWStr olarak marshal et — DLL wchar_t* bekliyor.
    /// flag: 0 (kullanim amaci bilinmiyor, hep 0 geciyoruz)
    /// hwId: 20 karakterlik donanim kimligi
    /// password: kullanicinin AnyOTP sifresi
    /// codeAddress: cikti — DLL OTP'yi memory'sine yazip pointer'i buraya koyar
    /// return: kullanmiyoruz (genelde 0)</summary>
    [UnmanagedFunctionPointer(CallingConvention.StdCall, CharSet = CharSet.Unicode)]
    delegate int GenerateOtpDelegate(
        int flag,
        [MarshalAs(UnmanagedType.LPWStr)] string hwId,
        [MarshalAs(UnmanagedType.LPWStr)] string password,
        out int codeAddress);

    // -------------------------------------------------------------------------
    // YARDIMCI: HATA YAZ + EXIT
    // -------------------------------------------------------------------------

    /// <summary>Hata mesajini stderr'e yaz, exit code dondur.</summary>
    static int Fail(string msg, int code) {
        Console.Error.WriteLine(msg);
        return code;
    }

    // -------------------------------------------------------------------------
    // STRING TEMIZLEME (BOM, ZWNJ, TIRNAK ICIN ROBUSTNESS)
    // -------------------------------------------------------------------------

    /// <summary>BOM, sifir-genislik bosluklar, baslangic/son tirnak ve whitespace temizler.
    ///
    /// CopyOTP_V2 ve benzeri araclar SpoofInfo.txt'yi UTF-16LE BOM'lu yazabiliyor.
    /// Bazi araclar tirnak icine alip yaziyor: "5616BD2F"
    /// Bazi kullanicilar manuel kopyalarken zero-width karakter tasiyor (ZWSP, RTL marks vb.).
    /// Bu fonksiyon tum bunlari temizleyip "saf" string birakir.
    /// </summary>
    static string StripJunk(string s) {
        if (s == null) return "";
        s = s.Trim();
        // BOM (﻿), zero-width space (​), LRM (‎), RLM (‏)
        // ve normal whitespace karakterleri.
        s = s.Trim('﻿', '​', '‎', '‏', ' ', '\t');
        // Cevredeki ASCII tirnak isaretleri (bazi araclar yaziyor)
        if (s.Length >= 2 &&
            ((s[0] == '"'  && s[s.Length - 1] == '"') ||
             (s[0] == '\'' && s[s.Length - 1] == '\''))) {
            s = s.Substring(1, s.Length - 2);
        }
        return s.Trim();
    }

    // -------------------------------------------------------------------------
    // SIGNATURE PARSE (decimal, hex, 0x prefix, negatif signed hepsi destekli)
    // -------------------------------------------------------------------------

    /// <summary>Signature 32-bit unsigned int. Decimal, hex (0x prefix veya plain),
    /// negatif signed hepsini kabul eder.
    ///
    /// Disk signature aslinda DWORD (uint32), 0..4,294,967,295 araliginda.
    /// Ama:
    ///   - C#'in int.Parse'i sadece signed (-2.1 milyar ... +2.1 milyar) destekler
    ///   - Bazi araclar hex yaziyor: "DEADBEEF"
    ///   - Bazi araclar 0x prefix ekliyor: "0xDEADBEEF"
    ///   - Bazi araclar int'in unsigned reinterpret'ini yaziyor: "-559038737"
    ///     (bu 0xDEADBEEF'in signed gosterimi)
    ///
    /// Hepsini dogru parse etmek icin sirayla deniyoruz:
    ///   1. "0x..." -> hex parse
    ///   2. duz decimal uint -> uint.Parse
    ///   3. negatif decimal int -> int.Parse, sonra unchecked cast
    ///   4. yalniz hex karakter (1-8 hane) -> hex parse
    /// </summary>
    static bool TryParseSignature(string s, out uint value) {
        value = 0;
        if (string.IsNullOrEmpty(s)) return false;

        // 1) "0x" veya "0X" prefix'li hex
        if (s.Length > 2 && (s.StartsWith("0x") || s.StartsWith("0X"))) {
            return uint.TryParse(s.Substring(2),
                System.Globalization.NumberStyles.HexNumber,
                System.Globalization.CultureInfo.InvariantCulture, out value);
        }

        // 2) Duz decimal uint (4 milyara kadar)
        if (uint.TryParse(s, System.Globalization.NumberStyles.Integer,
                System.Globalization.CultureInfo.InvariantCulture, out value))
            return true;

        // 3) Negatif signed olarak parse, sonra unsigned reinterpret
        // Ornegin "-559038737" -> 0xDEADBEEF (uint)
        int signed;
        if (int.TryParse(s, System.Globalization.NumberStyles.Integer,
                System.Globalization.CultureInfo.InvariantCulture, out signed)) {
            // unchecked cast — overflow exception atmasin
            value = unchecked((uint)signed);
            return true;
        }

        // 4) Prefix-siz hex (sadece 0-9 a-f A-F karakterleri, max 8 hane = 32 bit)
        bool allHex = true;
        for (int i = 0; i < s.Length; i++) {
            char c = s[i];
            if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F'))) {
                allHex = false; break;
            }
        }
        if (allHex && s.Length > 0 && s.Length <= 8) {
            return uint.TryParse(s, System.Globalization.NumberStyles.HexNumber,
                System.Globalization.CultureInfo.InvariantCulture, out value);
        }

        return false;
    }

    // -------------------------------------------------------------------------
    // ANA AKIS
    // -------------------------------------------------------------------------

    static int Main(string[] args) {
        // === Adim 1: Argumanlari kontrol et ===========================
        if (args.Length < 3) {
            return Fail("Usage: otp_core <dll_path> <spoof_path> <password>", 1);
        }
        string dllPath = args[0];
        string spoofPath = args[1];
        string password = args[2];

        if (!File.Exists(dllPath))   return Fail("DLL bulunamadi: " + dllPath, 2);
        if (!File.Exists(spoofPath)) return Fail("SpoofInfo bulunamadi: " + spoofPath, 3);

        // === Adim 2: SpoofInfo.txt'yi oku =============================
        // 3 satir bekliyoruz:
        //   [0] PNP device ID, ornegin: SCSI\DISK&VEN_NVME&PROD_VMWARE_VIRTUAL_N\5&25A13950&0&000000
        //   [1] disk signature (uint32)
        //   [2] tail (genelde 0, kullanmiyoruz)
        string[] lines = File.ReadAllLines(spoofPath);
        if (lines.Length < 2) return Fail("SpoofInfo format hatali (min 2 satir).", 4);

        // BOM, sifir-genislikli karakter ve cevreleyen tirnak/bosluklari ayikla
        string pnpId = StripJunk(lines[0]);
        string sigStr = StripJunk(lines[1]);

        uint signature;
        if (!TryParseSignature(sigStr, out signature))
            return Fail("Signature sayi degil: '" + sigStr + "'", 5);
        if (pnpId.Length < 16) return Fail("PNP ID cok kisa (<16).", 6);

        // === Adim 3: hardware_id'yi olustur ===========================
        // Algoritma orijinal otologin.py _otp_kopya'dan birebir alinmistir:
        //   text2 = format(int(signature), "X").zfill(4)   # signature'in hex'i, 4 hane padding
        //   str_part = pnp_id[-16:]                         # PNP ID'nin son 16 karakteri
        //   hardware_id = text2[:4] + str_part              # 4 + 16 = 20 karakter
        //
        // Ornek:
        //   signature = 90319493 (decimal) = 5616BD05 (hex)
        //   text2 = "5616BD05", text2[:4] = "5616"
        //   pnp_id[-16:] = "&0&0.0.0........" (son 16 karakter)
        //   hardware_id = "5616&0&0.0.0........"
        string sigHex = signature.ToString("X").PadLeft(4, '0');
        string hwIdPrefix = sigHex.Substring(0, 4);
        string hwIdSuffix = pnpId.Substring(pnpId.Length - 16);
        string hardwareId = hwIdPrefix + hwIdSuffix;

        // === Adim 4: DLL'i yukle ======================================
        // LoadLibraryExW'in mutlak yol istemesi gibi bir kural yok, AMA
        // LOAD_WITH_ALTERED_SEARCH_PATH flag'i sadece mutlak yol verirsen
        // calisiyor. Yoksa Windows hala default search order'i kullaniyor
        // ve DLL'in kendi klasorunde bagimliliklari aramiyor — sonuc "DLL load
        // failed" (error 126).
        string absDll = Path.GetFullPath(dllPath);
        IntPtr hMod = LoadLibraryExW(absDll, IntPtr.Zero, LOAD_FLAGS);
        if (hMod == IntPtr.Zero) {
            int err = Marshal.GetLastWin32Error();
            return Fail("LoadLibraryExW hata: " + err, 7);
        }

        try {
            // === Adim 5: GenerateOTP fonksiyon pointer'ini bul ========
            // DLL base + FUNCTION_OFFSET = fonksiyon adresi.
            // 32-bit derleme oldugumuz icin IntPtr 4 byte, ToInt32 guvenli.
            // Eger 64-bit derleseydik ToInt64 + offset gerekirdi.
            IntPtr fnPtr = new IntPtr(hMod.ToInt32() + FUNCTION_OFFSET);
            // Native fonksiyon pointer'ini managed delegate'e cevir.
            GenerateOtpDelegate genOtp = (GenerateOtpDelegate)
                Marshal.GetDelegateForFunctionPointer(fnPtr, typeof(GenerateOtpDelegate));

            // === Adim 6: GenerateOTP'yi cagir =========================
            // DLL OTP'yi kendi memory'sine yazar, sonra adresi codeAddress
            // out-parameter'ina koyar. Bu memory'i biz okumak zorundayiz.
            int codeAddress;
            genOtp(0, hardwareId, password, out codeAddress);

            if (codeAddress == 0) return Fail("GenerateOTP NULL adres dondu.", 8);

            // === Adim 7: codeAddress'tan OTP'yi oku ===================
            // DLL'in yazdigi adres bizim process'imizin icinde (DLL bizim
            // process'imize yuklendi). ReadProcessMemory teknik olarak
            // unnecessary — Marshal.Copy daha basit olurdu — ama orijinal Python
            // kodunu birebir taklit etmek icin bu yolu kullaniyoruz.
            IntPtr buf = Marshal.AllocHGlobal(OTP_BUFFER_BYTES);
            try {
                int bytesRead;
                bool ok = ReadProcessMemory(
                    GetCurrentProcess(),
                    new IntPtr(codeAddress),
                    buf, OTP_BUFFER_BYTES, out bytesRead);
                if (!ok) {
                    int err = Marshal.GetLastWin32Error();
                    return Fail("ReadProcessMemory hata: " + err, 9);
                }

                // 12 byte'i managed array'e kopyala, UTF-16'dan string'e cevir
                byte[] bytes = new byte[OTP_BUFFER_BYTES];
                Marshal.Copy(buf, bytes, 0, OTP_BUFFER_BYTES);
                // Sondaki null padding'i ve olasi whitespace'i temizle
                string otp = Encoding.Unicode.GetString(bytes).TrimEnd('\0').Trim();

                // === Adim 8: Saglik kontrolu ==========================
                // "000000" DLL'in "uretemedim" sinyali. Bu yanlis hardware_id
                // veya yanlis password'de oluyor — server-side dogrulamadan
                // ONCE local olarak da yakalanabilen tek hata sinyali.
                if (string.IsNullOrEmpty(otp) || otp == "000000")
                    return Fail("Gecerli OTP uretilemedi: '" + otp + "'", 10);

                // === Adim 9: Basari ===================================
                Console.WriteLine(otp);  // stdout'a 6 hane + newline
                return 0;
            } finally {
                // RAII: AllocHGlobal'in karsiligi FreeHGlobal — leak olmasin
                Marshal.FreeHGlobal(buf);
            }
        } finally {
            // RAII: LoadLibrary'nin karsiligi FreeLibrary — DLL ref count'u dusur
            FreeLibrary(hMod);
        }
    }
}
