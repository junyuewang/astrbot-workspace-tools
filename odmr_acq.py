"""
ODMR 數據採集程式 — Andor iDus 401 + Siglent SSG3000X
- 每個頻率點採集 MW_on / MW_off 各 N 幀
- AOM 快門自動控制 (曝光 HIGH, 讀出 LOW)
- MW 功率上限保護 (≤3 dBm)
- 記憶體平均後存一張 TIFF
- 自動命名存檔

依賴: pylablib, pyvisa, tifffile, numpy
"""

import ctypes
import sys
import time
import threading
import numpy as np
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime

try:
    import tifffile
except ImportError:
    print("請安裝 tifffile: pip install tifffile")
    sys.exit(1)

try:
    import pyvisa
except ImportError:
    print("請安裝 pyvisa: pip install pyvisa")
    sys.exit(1)

from pylablib.devices import Andor

# ============================================================
#  Andor SDK DLL
# ============================================================
_ANDOR_DLL_PATH = r"C:\Program Files\Andor SDK\atmcd64d.dll"
try:
    _andor_dll = ctypes.cdll.LoadLibrary(_ANDOR_DLL_PATH)
except OSError:
    _andor_dll = None
    print(f"警告: 找不到 Andor SDK DLL ({_ANDOR_DLL_PATH})")


# ============================================================
#  信號源控制
# ============================================================
class SignalGenerator:
    """Siglent SSG3000X via USB (VISA)"""

    def __init__(self, visa_addr):
        self.visa_addr = visa_addr
        self.rm = None
        self.instr = None

    def connect(self):
        self.rm = pyvisa.ResourceManager()
        self.instr = self.rm.open_resource(self.visa_addr)
        self.instr.timeout = 3000
        return self.query_idn()

    def disconnect(self):
        if self.instr:
            self.instr.close()
        if self.rm:
            self.rm.close()

    def query_idn(self):
        return self.instr.query("*IDN?").strip()

    def set_rf_output(self, on: bool):
        cmd = f":OUTPut:STATe {'ON' if on else 'OFF'}"
        self.instr.write(cmd)

    def set_frequency(self, freq_hz):
        self.instr.write(f":SOURce:FREQuency {freq_hz}")

    def set_power(self, power_dbm):
        self.instr.write(f":SOURce:POWer {power_dbm}")


# ============================================================
#  CCD 控制
# ============================================================
def init_camera(target_temp, tolerance):
    """初始化相機、降溫"""
    num = Andor.get_cameras_number_SDK2()
    if num == 0:
        raise RuntimeError("未偵測到 Andor SDK2 相機")
    cam = Andor.AndorSDK2Camera()
    cam.set_cooler(True)
    cam.set_temperature(target_temp)
    start = time.time()
    while True:
        t = cam.get_temperature()
        if abs(t - target_temp) <= tolerance:
            break
        if time.time() - start > 600:
            break
        time.sleep(2)
    return cam


def setup_camera(cam, exposure):
    """設定曝光、增益、快門"""
    cam.set_exposure(exposure)
    cam.set_read_mode("image")
    cam.set_trigger_mode("int")
    if _andor_dll is not None:
        _andor_dll.SetPreAmpGain(0)          # 高動態範圍
        _andor_dll.SetShutter(1, 0, 1, 1)    # TTL HIGH=曝光開, 全自動


def extract_center_roi(frame, roi_size=127):
    h, w = frame.shape
    r0 = (h - roi_size) // 2
    c0 = (w - roi_size) // 2
    return frame[r0:r0 + roi_size, c0:c0 + roi_size]


def acquire_frame(cam):
    """拍一幀並回傳中央 127×127 ROI"""
    frame = cam.snap()
    if frame is None:
        return None
    return extract_center_roi(frame, 127)


# ============================================================
#  ODMR 採集引擎
# ============================================================
def run_odmr(progress_cb, log_cb, done_cb,
             visa_addr, freq_start, freq_stop, n_freq,
             target_temp, tolerance, exposure,
             n_avg, mw_power_dbm, output_dir):
    total_points = n_freq * n_avg
    point = 0

    # ---- 頻率列表 ----
    if n_freq == 1:
        freq_list = [freq_start]
    else:
        freq_list = np.linspace(freq_start, freq_stop, n_freq)

    # ---- 連接信號源 ----
    log_cb("連接信號源…")
    sg = SignalGenerator(visa_addr)
    try:
        sg.connect()
        log_cb(f"信號源: {sg.query_idn()}")
    except Exception as e:
        log_cb(f"信號源連接失敗: {e}")
        done_cb()
        return
    sg.set_rf_output(True)
    sg.set_power(mw_power_dbm)

    # ---- 初始化相機 ----
    log_cb(f"降溫至 {target_temp}°C…")
    try:
        cam = init_camera(target_temp, tolerance)
        log_cb(f"溫度已穩定: {cam.get_temperature():.1f}°C")
    except Exception as e:
        log_cb(f"相機初始化失敗: {e}")
        sg.set_rf_output(False)
        sg.disconnect()
        done_cb()
        return

    setup_camera(cam, exposure)

    # ---- 開始採集 ----
    log_cb(f"開始 ODMR: {freq_start:.0f}–{freq_stop:.0f} MHz, "
           f"{n_freq} 點, {n_avg} 次平均, {exposure}s 曝光")
    t_start = time.time()

    for i_freq, freq_mhz in enumerate(freq_list):
        freq_hz = freq_mhz * 1e6

        acc_on = None
        acc_off = None

        for i_avg in range(n_avg):
            # --- MW ON ---
            sg.set_frequency(freq_hz)
            sg.set_rf_output(True)
            time.sleep(0.05)

            frame_on = acquire_frame(cam)
            if frame_on is not None:
                f_on = frame_on.astype(np.float64)
                acc_on = f_on if acc_on is None else acc_on + f_on

            # --- MW OFF ---
            sg.set_rf_output(False)
            time.sleep(0.01)

            frame_off = acquire_frame(cam)
            if frame_off is not None:
                f_off = frame_off.astype(np.float64)
                acc_off = f_off if acc_off is None else acc_off + f_off

            point += 1
            progress_cb(point, total_points)

        # 儲存該頻率點的平均幀
        if acc_on is not None:
            avg_on = (acc_on / n_avg).astype(np.uint16)
            fn = f"{output_dir}/odmr_f{i_freq:04d}_{freq_mhz:.2f}MHz_on.tiff"
            tifffile.imwrite(fn, avg_on, photometric='minisblack')

        if acc_off is not None:
            avg_off = (acc_off / n_avg).astype(np.uint16)
            fn = f"{output_dir}/odmr_f{i_freq:04d}_{freq_mhz:.2f}MHz_off.tiff"
            tifffile.imwrite(fn, avg_off, photometric='minisblack')

        elapsed = time.time() - t_start
        eta = elapsed / (i_freq + 1) * (n_freq - i_freq - 1)
        log_cb(f"[{i_freq+1}/{n_freq}] {freq_mhz:.1f} MHz 完成, "
               f"已過 {elapsed:.0f}s, 預計剩餘 {eta:.0f}s")

    # ---- 清理 ----
    sg.set_rf_output(False)
    sg.disconnect()
    cam.close()

    total_time = time.time() - t_start
    log_cb(f"採集完成！總耗時 {total_time:.0f}s ({total_time/60:.1f}分)")
    done_cb()


# ============================================================
#  GUI
# ============================================================
class OdmrApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ODMR 數據採集 (iDus 401 + SSG3000X)")
        self.geometry("600x750")
        self.resizable(True, True)

        # ---- 信號源 ----
        f_sg = ttk.LabelFrame(self, text="信號源 (SSG3000X)")
        f_sg.pack(pady=8, padx=10, fill="x")

        ttk.Label(f_sg, text="VISA 位址:").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        self.visa_addr = ttk.Entry(f_sg, width=45)
        self.visa_addr.insert(0, "USB0::0xF4EC::0x1501::SSG3XGCD3R0206::INSTR")
        self.visa_addr.grid(row=0, column=1, padx=5, pady=2)

        ttk.Label(f_sg, text="MW 功率 (dBm):").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        self.mw_power = ttk.Entry(f_sg, width=10)
        self.mw_power.insert(0, "0")
        self.mw_power.grid(row=1, column=1, padx=5, pady=2, sticky="w")

        ttk.Label(f_sg, text="(最大 3 dBm, 保護功放)").grid(row=1, column=2, sticky="w", padx=2, pady=2)

        # ---- 掃頻 ----
        f_freq = ttk.LabelFrame(self, text="掃頻參數")
        f_freq.pack(pady=8, padx=10, fill="x")

        ttk.Label(f_freq, text="開始頻率 (MHz):").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        self.freq_start = ttk.Entry(f_freq, width=12)
        self.freq_start.insert(0, "2800")
        self.freq_start.grid(row=0, column=1, padx=5, pady=2)

        ttk.Label(f_freq, text="結束頻率 (MHz):").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        self.freq_stop = ttk.Entry(f_freq, width=12)
        self.freq_stop.insert(0, "2950")
        self.freq_stop.grid(row=1, column=1, padx=5, pady=2)

        ttk.Label(f_freq, text="頻率點數:").grid(row=2, column=0, sticky="w", padx=5, pady=2)
        self.n_freq = ttk.Entry(f_freq, width=12)
        self.n_freq.insert(0, "100")
        self.n_freq.grid(row=2, column=1, padx=5, pady=2)

        # ---- 相機 ----
        f_cam = ttk.LabelFrame(self, text="相機參數")
        f_cam.pack(pady=8, padx=10, fill="x")

        ttk.Label(f_cam, text="目標溫度 (°C):").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        self.target_temp = ttk.Entry(f_cam, width=10)
        self.target_temp.insert(0, "-20")
        self.target_temp.grid(row=0, column=1, padx=5, pady=2)

        ttk.Label(f_cam, text="溫度容差 (°C):").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        self.temp_tol = ttk.Entry(f_cam, width=10)
        self.temp_tol.insert(0, "2")
        self.temp_tol.grid(row=1, column=1, padx=5, pady=2)

        ttk.Label(f_cam, text="曝光時間 (秒):").grid(row=2, column=0, sticky="w", padx=5, pady=2)
        self.exposure = ttk.Entry(f_cam, width=10)
        self.exposure.insert(0, "2.0")
        self.exposure.grid(row=2, column=1, padx=5, pady=2)

        ttk.Label(f_cam, text="每點平均次數:").grid(row=3, column=0, sticky="w", padx=5, pady=2)
        self.n_avg = ttk.Entry(f_cam, width=10)
        self.n_avg.insert(0, "1")
        self.n_avg.grid(row=3, column=1, padx=5, pady=2)

        # ---- 輸出 ----
        f_out = ttk.LabelFrame(self, text="輸出目錄")
        f_out.pack(pady=8, padx=10, fill="x")

        self.output_dir = ttk.Entry(f_out, width=45)
        self.output_dir.insert(0, "./odmr_data")
        self.output_dir.grid(row=0, column=0, padx=5, pady=2)

        # ---- 控制 ----
        f_ctrl = ttk.Frame(self)
        f_ctrl.pack(pady=10)

        self.btn_start = ttk.Button(f_ctrl, text="▶ 開始 ODMR", command=self.start)
        self.btn_start.pack(side="left", padx=8)

        self.btn_abort = ttk.Button(f_ctrl, text="■ 中止", command=self.abort,
                                    state="disabled")
        self.btn_abort.pack(side="left", padx=8)

        # ---- 進度 ----
        f_prog = ttk.LabelFrame(self, text="進度")
        f_prog.pack(pady=8, padx=10, fill="x")

        self.progress_bar = ttk.Progressbar(f_prog, length=500, mode="determinate")
        self.progress_bar.pack(pady=5)

        self.label_progress = ttk.Label(f_prog, text="就緒", font=("Arial", 11))
        self.label_progress.pack(pady=2)

        # ---- 日誌 ----
        f_log = ttk.LabelFrame(self, text="日誌")
        f_log.pack(pady=8, padx=10, fill="both", expand=True)

        self.log_text = tk.Text(f_log, height=10, font=("Consolas", 9))
        self.log_text.pack(pady=5, padx=5, fill="both", expand=True)

        # 狀態
        self.running = False
        self.thread = None
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ============================================================
    def log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {msg}\n")
        self.log_text.see("end")

    def update_progress(self, current, total):
        self.progress_bar["maximum"] = total
        self.progress_bar["value"] = current
        self.label_progress.config(text=f"{current}/{total} ({current/total*100:.0f}%)")

    # ============================================================
    def start(self):
        if self.running:
            return

        try:
            visa = self.visa_addr.get().strip()
            mw_power = float(self.mw_power.get())
            if mw_power > 3.0:
                messagebox.showerror("安全保護", "MW 功率不能超過 3 dBm，否則可能燒毀功放！")
                return
            f_start = float(self.freq_start.get())
            f_stop = float(self.freq_stop.get())
            n_f = int(self.n_freq.get())
            temp = float(self.target_temp.get())
            tol = float(self.temp_tol.get())
            exp = float(self.exposure.get())
            n_avg = int(self.n_avg.get())
            out_dir = self.output_dir.get().strip()
        except ValueError as e:
            messagebox.showerror("參數錯誤", str(e))
            return

        if n_f < 1:
            messagebox.showerror("錯誤", "頻率點數必須 ≥1")
            return
        if exp <= 0:
            messagebox.showerror("錯誤", "曝光時間必須 >0")
            return

        import os
        os.makedirs(out_dir, exist_ok=True)

        self.btn_start.config(state="disabled")
        self.btn_abort.config(state="normal")
        self.running = True
        self.progress_bar["value"] = 0
        self.log_text.delete("1.0", "end")

        self.thread = threading.Thread(
            target=run_odmr,
            args=(
                lambda c, t: self.after(0, self.update_progress, c, t),
                lambda m: self.after(0, self.log, m),
                lambda: self.after(0, self._on_done),
                visa, f_start, f_stop, n_f,
                temp, tol, exp, n_avg, mw_power, out_dir
            ),
            daemon=True
        )
        self.thread.start()

    def abort(self):
        self.running = False
        self.log("⚠ 使用者中止")
        self._on_done()

    def _on_done(self):
        self.btn_abort.config(state="disabled")
        self.btn_start.config(state="normal")
        self.running = False

    def on_close(self):
        self.running = False
        self.destroy()


# ============================================================
if __name__ == "__main__":
    app = OdmrApp()
    app.mainloop()
