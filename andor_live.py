"""
Andor iDus 401 CCD 調光路工具
- 設定溫度、曝光時間
- 播放：連續採集並顯示中央 127×127 ROI（4× 放大）
- 停止：暫停採集
- 保存：直接存為 TIFF（自動命名），同時顯示 ROI 最大值與均值
- AOM 常開：雷射常亮（輔助相機找樣品用）
- 快門 TTL：曝光 HIGH, 讀出 LOW（接 AOM 消除 smear）

增益：高動態模式 (Gain 0), 有效滿井 ~36,100 counts
最佳曝光：2 s, mean ~19k counts
"""

import ctypes
import sys
import time
import threading
import numpy as np
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk

try:
    import tifffile
except ImportError:
    print("請安裝 tifffile: pip install tifffile")
    sys.exit(1)

from pylablib.devices import Andor

# Andor SDK2 DLL — 使用絕對路徑
_ANDOR_DLL_PATH = r"C:\Program Files\Andor SDK\atmcd64d.dll"
try:
    _andor_dll = ctypes.cdll.LoadLibrary(_ANDOR_DLL_PATH)
except OSError:
    _andor_dll = None
    print(f"警告: 找不到 Andor SDK DLL ({_ANDOR_DLL_PATH})，快門控制不可用")


# ================================================================
#  輔助函數
# ================================================================

def set_temperature_with_callback(cam, target_temp, tolerance, callback):
    """降溫並透過 callback 回報當前溫度，直到穩定"""
    cam.set_cooler(True)
    cam.set_temperature(target_temp)
    start = time.time()
    while True:
        current = cam.get_temperature()
        callback(current)
        if abs(current - target_temp) <= tolerance:
            break
        if time.time() - start > 600:          # 最多等 10 分鐘
            break
        time.sleep(2)


def extract_center_roi(frame, roi_size=127):
    """從原始幀截取中央 roi_size × roi_size 區域"""
    h, w = frame.shape
    if h < roi_size or w < roi_size:
        raise ValueError(f"影像尺寸 ({w}×{h}) 小於 ROI ({roi_size}×{roi_size})")
    r0 = (h - roi_size) // 2
    c0 = (w - roi_size) // 2
    return frame[r0:r0 + roi_size, c0:c0 + roi_size]


def scale_to_display(roi):
    """將 16-bit ROI 線性拉伸到 0–255（min/max）"""
    vmin, vmax = roi.min(), roi.max()
    if vmin == vmax:
        vmin -= 1
        vmax += 1
    scaled = (roi.astype(np.float32) - vmin) / (vmax - vmin) * 255.0
    return np.clip(scaled, 0, 255).astype(np.uint8)


def default_filename():
    """產生時間戳檔名"""
    return time.strftime("roi_%Y%m%d_%H%M%S.tiff")


# ================================================================
#  GUI
# ================================================================

class AndorLiveApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Andor iDus 401 調光路工具")
        self.geometry("680x850")
        self.resizable(True, True)

        # ----- 內部狀態 -----
        self.cam = None
        self.running = False          # 播放中？
        self.lock = threading.Lock()
        self.current_roi = None       # 最新一幀 ROI
        self.current_raw = None       # 最新一幀原始資料（用於顯示統計）
        self.photo = None
        self.aom_always_on = False    # AOM 常開模式

        # ----- 相機參數區 -----
        frame_cam = ttk.LabelFrame(self, text="相機參數")
        frame_cam.pack(pady=10, padx=10, fill="x")

        ttk.Label(frame_cam, text="目標溫度 (°C):").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        self.entry_temp = ttk.Entry(frame_cam, width=10)
        self.entry_temp.insert(0, "-60")
        self.entry_temp.grid(row=0, column=1, padx=5, pady=2)

        ttk.Label(frame_cam, text="溫度容差 (°C):").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        self.entry_tol = ttk.Entry(frame_cam, width=10)
        self.entry_tol.insert(0, "4")                          # 預設 4°C
        self.entry_tol.grid(row=1, column=1, padx=5, pady=2)

        ttk.Label(frame_cam, text="曝光時間 (秒):").grid(row=2, column=0, sticky="w", padx=5, pady=2)
        self.entry_exp = ttk.Entry(frame_cam, width=10)
        self.entry_exp.insert(0, "2.0")                        # 預設 2 s
        self.entry_exp.grid(row=2, column=1, padx=5, pady=2)

        ttk.Label(frame_cam, text="快門 TTL (SMB pin3):").grid(row=3, column=0, sticky="w", padx=5, pady=2)
        self.var_shutter = tk.BooleanVar(value=True)
        self.chk_shutter = ttk.Checkbutton(frame_cam, variable=self.var_shutter,
                                           text="啟用（曝光 HIGH, 讀出 LOW）")
        self.chk_shutter.grid(row=3, column=1, padx=5, pady=2, sticky="w")

        # ----- 狀態顯示 -----
        frame_stat = ttk.LabelFrame(self, text="狀態")
        frame_stat.pack(pady=10, padx=10, fill="x")

        self.label_temp = ttk.Label(frame_stat, text="當前溫度: -- °C", font=("Arial", 12))
        self.label_temp.pack(pady=2)

        self.label_stats = ttk.Label(frame_stat, text="ROI: max= -- , mean= --",
                                     font=("Arial", 12, "bold"))
        self.label_stats.pack(pady=2)

        self.label_status = ttk.Label(frame_stat, text="就緒", font=("Arial", 11))
        self.label_status.pack(pady=2)

        # ----- 按鈕 -----
        frame_btn = ttk.Frame(self)
        frame_btn.pack(pady=10)

        self.btn_play = ttk.Button(frame_btn, text="▶ 播放", command=self.start_live)
        self.btn_play.pack(side="left", padx=8)

        self.btn_stop = ttk.Button(frame_btn, text="■ 停止", command=self.stop_live,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=8)

        self.btn_save = ttk.Button(frame_btn, text="💾 保存", command=self.save_current)
        self.btn_save.pack(side="left", padx=8)

        self.btn_aom_on = ttk.Button(frame_btn, text="🔦 AOM 常開",
                                     command=self.toggle_aom_always_on)
        self.btn_aom_on.pack(side="left", padx=8)

        # ----- 圖像顯示 -----
        frame_img = ttk.LabelFrame(self, text="中央 ROI (127×127) — 4× 放大")
        frame_img.pack(pady=10, padx=10, fill="both", expand=True)

        self.label_image = ttk.Label(frame_img, background="black")
        self.label_image.pack()

        # ----- 清理 -----
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    # ============================================================
    #  AOM 常開（輔助相機找樣品用）
    # ============================================================
    def toggle_aom_always_on(self):
        self.aom_always_on = not self.aom_always_on
        if _andor_dll is None:
            self._update_status("快門 DLL 未載入")
            return

        # 確保相機已初始化
        if self.cam is None:
            try:
                num = Andor.get_cameras_number_SDK2()
                if num == 0:
                    self._update_status("未偵測到 Andor SDK2 相機")
                    return
                self.cam = Andor.AndorSDK2Camera()
                self.cam.set_cooler(False)   # AOM 常開不需要降溫
                self._update_status("相機已初始化")
            except Exception as e:
                self._update_status(f"相機初始化失敗: {e}")
                return

        try:
            if self.aom_always_on:
                _andor_dll.SetShutter(1, 1, 1, 1)
                self.btn_aom_on.config(text="🔦 AOM 常開 [ON]")
                self._update_status("AOM 常開 — 輔助相機可用")
            else:
                _andor_dll.SetShutter(1, 2, 1, 1)
                self.btn_aom_on.config(text="🔦 AOM 常開")
                self._update_status("AOM 關閉 — 按播放或再次點擊開啟")
        except Exception as e:
            self._update_status(f"AOM 切換失敗: {e}")

    # ============================================================
    #  播放
    # ============================================================
    def start_live(self):
        if self.running:
            return

        # 讀取參數
        try:
            target_temp = float(self.entry_temp.get())
        except ValueError:
            messagebox.showerror("錯誤", "目標溫度必須為數字")
            return
        try:
            tolerance = float(self.entry_tol.get())
        except ValueError:
            messagebox.showerror("錯誤", "溫度容差必須為數字")
            return
        try:
            exposure = float(self.entry_exp.get())
            if exposure <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("錯誤", "曝光時間必須為正數")
            return

        self.btn_play.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.running = True

        # 播放時自動退出 AOM 常開模式
        if self.aom_always_on:
            self.aom_always_on = False
            self.btn_aom_on.config(text="🔦 AOM 常開")

        thread = threading.Thread(target=self._live_thread,
                                  args=(target_temp, tolerance, exposure),
                                  daemon=True)
        thread.start()

    def _live_thread(self, target_temp, tolerance, exposure):
        """在背景執行：初始化相機 → 降溫 → 連續採集"""
        try:
            # 1. 連接相機（若尚未連接）
            if self.cam is None:
                num = Andor.get_cameras_number_SDK2()
                if num == 0:
                    self._update_status("未偵測到 Andor SDK2 相機")
                    self._finish()
                    return
                self.cam = Andor.AndorSDK2Camera()
                self._update_status("相機已連接，降溫中…")

            # 2. 降溫
            set_temperature_with_callback(
                self.cam, target_temp, tolerance,
                lambda t: self.after(0, self._update_temp, t)
            )
            self._update_status("溫度已穩定，開始播放")

            # 3. 設定曝光與增益
            self.cam.set_exposure(exposure)
            _andor_dll.SetPreAmpGain(0)      # 高動態範圍
            self.cam.set_read_mode("image")
            self.cam.set_trigger_mode("int")

            # 4. 快門 TTL 控制
            shutter_on = self.var_shutter.get()
            if shutter_on:
                try:
                    # Andor SDK: SetShutter(typ, mode, closingtime, openingtime)
                    # typ=1: TTL HIGH 曝光時開, mode=0: 全自動
                    if _andor_dll is not None:
                        _andor_dll.SetShutter(1, 0, 1, 1)
                        self._update_status("快門 TTL 已啟用 (曝光 HIGH, 讀出 LOW)")
                    else:
                        self._update_status("快門 DLL 未載入")
                except Exception as e:
                    self._update_status(f"快門設定失敗: {e}")
            else:
                self._update_status("快門 TTL 未啟用")

            # 5. 連續採集循環
            while self.running:
                frame = self.cam.snap()
                if frame is None:
                    continue

                roi = extract_center_roi(frame, roi_size=127)
                roi_float = roi.astype(np.float64)

                with self.lock:
                    self.current_roi = roi.copy()
                    self.current_raw = roi_float

                # 更新 GUI（max / mean）
                self.after(0, self._update_display, roi.copy(), roi_float.max(), roi_float.mean())

                # 更新溫度
                try:
                    t = self.cam.get_temperature()
                    self.after(0, self._update_temp, t)
                except Exception:
                    pass

        except Exception as e:
            self._update_status(f"錯誤: {e}")
        finally:
            self._finish()

    def _update_display(self, roi, vmax, vmean):
        """在 GUI 執行緒中更新圖像和統計"""
        img_u8 = scale_to_display(roi)
        img_pil = Image.fromarray(img_u8, 'L').resize((508, 508), Image.NEAREST)
        self.photo = ImageTk.PhotoImage(img_pil)
        self.label_image.config(image=self.photo)
        self.label_stats.config(text=f"ROI: max = {vmax:.0f},  mean = {vmean:.1f}")

    def _update_temp(self, t):
        self.label_temp.config(text=f"當前溫度: {t:.1f} °C")

    def _update_status(self, msg):
        self.after(0, lambda: self.label_status.config(text=msg))

    def _finish(self):
        """還原按鈕狀態"""
        self.after(0, self._reset_buttons)

    def _reset_buttons(self):
        self.btn_play.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.running = False

    # ============================================================
    #  停止
    # ============================================================
    def stop_live(self):
        self.running = False
        self.btn_stop.config(state="disabled")
        self.btn_play.config(state="normal")
        self.label_status.config(text="已停止（相機保持降溫）")

    # ============================================================
    #  保存
    # ============================================================
    def save_current(self):
        with self.lock:
            if self.current_roi is None:
                messagebox.showwarning("警告", "尚無可保存的圖像")
                return
            roi_to_save = self.current_roi.copy()

        fname = default_filename()
        try:
            tifffile.imwrite(fname, roi_to_save, photometric='minisblack')
            self.label_status.config(text=f"已保存: {fname}")
        except Exception as e:
            messagebox.showerror("保存失敗", str(e))

    # ============================================================
    #  退出清理
    # ============================================================
    def on_closing(self):
        self.running = False
        if self.cam is not None:
            try:
                self.cam.close()
            except Exception:
                pass
        self.destroy()


# ================================================================
#  主程式入口
# ================================================================
if __name__ == "__main__":
    app = AndorLiveApp()
    app.mainloop()
