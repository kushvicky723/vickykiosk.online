import os
import sys
import threading
import subprocess
import time
import customtkinter as ctk
from PIL import Image, ImageTk

# Windows Print Libraries for Printer Detection & Direct Printing
try:
    import win32print  # type: ignore
    import win32api     # type: ignore
except ImportError:
    win32print = None
    win32api = None

# QR Code Library for direct GUI generation
try:
    import qrcode
except ImportError:
    qrcode = None

# CustomTkinter Theme Setup
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

class VickyKioskApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        # Window Title & Configuration
        self.title("Vicky Smart Kiosk - Shop Control Panel")
        self.geometry("680x620")
        self.resizable(False, False)

        # App close hone par background server band ho
        self.protocol("WM_DELETE_WINDOW", self.on_app_close)

        self.server_process = None
        self.is_running = False
        self.total_prints_today = 0
        self.total_earnings_today = 0

        # Build Clean Professional UI
        self.create_widgets()

    def on_app_close(self):
        self.stop_server()
        self.destroy()

    def get_installed_printers(self):
        printers = []
        if win32print:
            try:
                printer_info = win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)
                for printer in printer_info:
                    printers.append(printer[2])
            except Exception:
                pass
        if not printers:
            printers = ["Default Windows Printer"]
        return printers

    def refresh_printers_list(self):
        printers = self.get_installed_printers()
        self.printer_dropdown.configure(values=printers)
        if printers:
            self.printer_dropdown.set(printers[0])
        self.update_activity("Printers list refreshed.")

    def create_widgets(self):
        # 1. Header Branding
        header_frame = ctk.CTkFrame(self, corner_radius=12, fg_color="#1a1c23")
        header_frame.pack(fill="x", padx=20, pady=(15, 10))

        title_label = ctk.CTkLabel(
            header_frame, 
            text="⚡ VICKY SMART PRINT KIOSK", 
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color="#00d2ff"
        )
        title_label.pack(pady=(12, 2))

        subtitle_label = ctk.CTkLabel(
            header_frame, 
            text="Automatic Phone-to-Printer Shop Manager", 
            font=ctk.CTkFont(size=12),
            text_color="#a0a5b5"
        )
        subtitle_label.pack(pady=(0, 12))

        # 2. Main Content Split
        content_frame = ctk.CTkFrame(self, fg_color="transparent")
        content_frame.pack(fill="both", expand=True, padx=20, pady=5)

        # Left Column - Settings & Stats
        left_col = ctk.CTkFrame(content_frame, fg_color="#22252f", corner_radius=12)
        left_col.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=5)

        self.status_label = ctk.CTkLabel(
            left_col, 
            text="STATUS: STOPPED 🔴", 
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#ff4d4d"
        )
        self.status_label.pack(pady=(15, 10))

        self.toggle_btn = ctk.CTkButton(
            left_col, 
            text="▶️ START KIOSK SERVER", 
            font=ctk.CTkFont(size=14, weight="bold"),
            height=42,
            fg_color="#00c853",
            hover_color="#00a743",
            command=self.toggle_server
        )
        self.toggle_btn.pack(fill="x", padx=15, pady=5)

        printer_header = ctk.CTkFrame(left_col, fg_color="transparent")
        printer_header.pack(fill="x", padx=15, pady=(15, 2))

        printer_title = ctk.CTkLabel(printer_header, text="🖨️ Select Connected Printer:", font=ctk.CTkFont(size=12, weight="bold"))
        printer_title.pack(side="left")

        refresh_btn = ctk.CTkButton(
            printer_header, text="🔄", width=28, height=24, fg_color="#37474f", hover_color="#455a64", command=self.refresh_printers_list
        )
        refresh_btn.pack(side="right")

        installed_printers = self.get_installed_printers()
        self.printer_dropdown = ctk.CTkOptionMenu(left_col, values=installed_printers, font=ctk.CTkFont(size=12))
        self.printer_dropdown.pack(fill="x", padx=15, pady=(0, 10))

        stats_frame = ctk.CTkFrame(left_col, fg_color="#1a1c23", corner_radius=10)
        stats_frame.pack(fill="x", padx=15, pady=10)

        stats_title = ctk.CTkLabel(stats_frame, text="📊 Today's Summary", font=ctk.CTkFont(size=12, weight="bold"), text_color="#00d2ff")
        stats_title.pack(pady=(8, 4))

        self.prints_count_label = ctk.CTkLabel(stats_frame, text="📄 Total Prints Today: 0", font=ctk.CTkFont(size=12))
        self.prints_count_label.pack(pady=2)

        self.earnings_label = ctk.CTkLabel(stats_frame, text="💰 Total Earning: ₹0", font=ctk.CTkFont(size=13, weight="bold"), text_color="#00e676")
        self.earnings_label.pack(pady=(2, 8))

        # Right Column - QR Code Display
        right_col = ctk.CTkFrame(content_frame, fg_color="#22252f", corner_radius=12, width=240)
        right_col.pack(side="right", fill="both", padx=(5, 0), pady=5)

        qr_title = ctk.CTkLabel(right_col, text="📱 Shop QR Code", font=ctk.CTkFont(size=14, weight="bold"))
        qr_title.pack(pady=(12, 5))

        self.qr_display = ctk.CTkLabel(right_col, text="Server Offline\nStart Server to view QR", font=ctk.CTkFont(size=11), width=180, height=180, fg_color="#1a1c23", corner_radius=10)
        self.qr_display.pack(pady=10, padx=15)

        self.print_qr_btn = ctk.CTkButton(
            right_col, text="🖨️ Print Shop QR Poster", font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#37474f", hover_color="#455a64", state="disabled", command=self.print_qr_poster
        )
        self.print_qr_btn.pack(fill="x", padx=15, pady=(5, 12))

        # 3. Footer Activity Log
        footer_frame = ctk.CTkFrame(self, fg_color="#1a1c23", corner_radius=10)
        footer_frame.pack(fill="x", padx=20, pady=(5, 15))

        self.activity_label = ctk.CTkLabel(footer_frame, text="System Status: Ready to start.", font=ctk.CTkFont(size=12), text_color="#888888")
        self.activity_label.pack(pady=8, padx=10)

    def update_activity(self, message):
        self.activity_label.configure(text=f"Activity: {message}", text_color="#ffffff")

    def toggle_server(self):
        if not self.is_running:
            self.start_server()
        else:
            self.stop_server()

    def start_server(self):
        self.is_running = True
        self.status_label.configure(text="STATUS: ONLINE & READY 🟢", text_color="#00e676")
        self.toggle_btn.configure(text="⏹️ STOP KIOSK SERVER", fg_color="#d50000", hover_color="#b71c1c")
        self.update_activity("Starting Server and Generating QR Code...")

        # 1. Background mein main.py chalayein taaki printing aur API chalu rahe
        threading.Thread(target=self.run_backend, daemon=True).start()

        # 2. Turant GUI ke andar Static Domain ka QR code bana kar screen par dikhayein
        try:
            if qrcode:
                STATIC_DOMAIN = "probable-goldmine-undefined.ngrok-free.dev"
                public_url = f"https://{STATIC_DOMAIN}"
                
                img = qrcode.make(public_url)
                img.save("dukan_qr_code.png")
                
                loaded_img = Image.open("dukan_qr_code.png")
                ctk_img = ctk.CTkImage(light_image=loaded_img, dark_image=loaded_img, size=(170, 170))
                self.qr_display.configure(image=ctk_img, text="")
                self.print_qr_btn.configure(state="normal")
                self.update_activity("✅ Shop QR Generated & Loaded Successfully!")
        except Exception as e:
            self.update_activity(f"QR Error: {e}")

    def print_qr_poster(self):
        qr_path = "dukan_qr_code.png"
        selected_printer = self.printer_dropdown.get()
        if os.path.exists(qr_path):
            try:
                if win32api:
                    win32api.ShellExecute(0, "print", qr_path, f'/d:"{selected_printer}"', ".", 0)
                else:
                    os.startfile(qr_path, "print")
                self.update_activity("Printed Shop QR Poster successfully!")
            except Exception as e:
                self.update_activity(f"Print Poster Error: {e}")

    def run_backend(self):
        try:
            python_exe = sys.executable
            selected_printer = self.printer_dropdown.get()
            
            env = os.environ.copy()
            env["SELECTED_PRINTER"] = selected_printer

            self.server_process = subprocess.Popen(
                [python_exe, "main.py"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env
            )
            for line in iter(self.server_process.stdout.readline, ''):
                if line:
                    clean_line = line.strip()
                    if "Total Bill" in clean_line:
                        try:
                            amount = int(clean_line.split("₹")[-1])
                            self.total_earnings_today += amount
                            self.total_prints_today += 1
                            self.prints_count_label.configure(text=f"📄 Total Prints Today: {self.total_prints_today}")
                            self.earnings_label.configure(text=f"💰 Total Earning: ₹{self.total_earnings_today}")
                            self.update_activity(f"New Order Printed! Earned: ₹{amount}")
                        except Exception:
                            pass
            self.server_process.stdout.close()
            self.server_process.wait()
        except Exception as e:
            self.update_activity(f"Server Error: {e}")

    def stop_server(self):
        if self.server_process:
            try:
                self.server_process.terminate()
            except:
                pass
            self.server_process = None
            
        self.is_running = False
        self.status_label.configure(text="STATUS: STOPPED 🔴", text_color="#ff4d4d")
        self.toggle_btn.configure(text="▶️ START KIOSK SERVER", fg_color="#00c853", hover_color="#00a743")
        self.qr_display.configure(image="", text="Server Offline\nStart Server to view QR")
        self.print_qr_btn.configure(state="disabled")
        self.update_activity("Kiosk Server Stopped.")

if __name__ == "__main__":
    app = VickyKioskApp()
    app.mainloop()