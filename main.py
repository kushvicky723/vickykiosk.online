import os
import sys
import subprocess
import socket
import time
import threading
import traceback
import json
import random
from datetime import datetime
from PIL import Image, ImageOps, ImageDraw, ImageFont

from fastapi import FastAPI, File, UploadFile, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from queue import Queue

# Pillow-HEIF for iPhone HEIC images support
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

# PyMuPDF for PDF to Image conversion
try:
    import fitz  # type: ignore
except ImportError:
    fitz = None

# Windows Printing Libraries
try:
    import win32print  # type: ignore
    import win32api     # type: ignore
except ImportError:
    pass

# QR Code Library
try:
    import qrcode  # type: ignore
except ImportError:
    qrcode = None

app = FastAPI(title="Vicky Smart Kiosk Enterprise")

# Global variables
SELECTED_PRINTER = "Default Printer"
TODAY_PRINT_COUNT = 0
CURRENT_SHOP_ID = "Unknown"
DB_FILE = "shop_database.json"

# Large PDF Approval Storage (Job ID -> Status)
PENDING_APPROVALS = {}

# Thread-Safe Print Queue for simultaneous requests handling
print_queue = Queue()

def print_worker():
    while True:
        task = print_queue.get()
        if task is None:
            break
        sumatra_path, printable_images, copies, printer_to_use = task
        try:
            for img_path in printable_images:
                try:
                    if printer_to_use and printer_to_use != "Default Printer":
                        cmd = f'"{sumatra_path}" -print-to "{printer_to_use}" -silent -print-settings "{copies}x,fit" "{img_path}"'
                        res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                        if res.returncode != 0:
                            raise Exception("Selected printer failed, fallback to default.")
                    else:
                        cmd = f'"{sumatra_path}" -print-to-default -silent -print-settings "{copies}x,fit" "{img_path}"'
                        subprocess.run(cmd, shell=True)
                except Exception as p_err:
                    print(f"[WARNING] Selected printer error, redirecting to Windows Default Printer: {p_err}")
                    fallback_cmd = f'"{sumatra_path}" -print-to-default -silent -print-settings "{copies}x,fit" "{img_path}"'
                    subprocess.run(fallback_cmd, shell=True)
            print("[SUCCESS] Print Queue Job Executed Successfully!")
        except Exception as e:
            print(f"[ERROR] Queue Printing Error: {e}")
        finally:
            print_queue.task_done()

threading.Thread(target=print_worker, daemon=True).start()

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

def get_pc_signature():
    try:
        return socket.gethostname() + "_" + os.environ.get("USERNAME", "user")
    except:
        return "default_pc_node"

@app.middleware("http")
async def catch_exceptions_middleware(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        print("\n--- [CRITICAL] CRITICAL BACKEND ERROR DETECTED ---")
        traceback.print_exc()
        print("-------------------------------------------\n")
        return JSONResponse(status_code=500, content={"detail": str(e)})

def auto_delete_files(file_paths, delay=120):
    def delete_job():
        time.sleep(delay)
        for path in file_paths:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                pass
    threading.Thread(target=delete_job, daemon=True).start()

def process_file_to_printable_images(file_path, print_type, page_selection="", layout_mode="normal"):
    output_images = []
    base_name, file_ext = os.path.splitext(file_path)
    file_ext = file_ext.lower().lstrip('.')

    if file_ext == 'pdf':
        if fitz is None:
            raise Exception("PyMuPDF (fitz) library install nahi hai!")
        
        doc = fitz.open(file_path)
        total_pages = len(doc)
        
        pages_to_process = []
        if page_selection and page_selection.strip():
            try:
                p_num = int(page_selection.strip())
                if 1 <= p_num <= total_pages:
                    pages_to_process = [p_num - 1]
                else:
                    pages_to_process = list(range(total_pages))
            except:
                pages_to_process = list(range(total_pages))
        else:
            pages_to_process = list(range(total_pages))

        extracted_page_paths = []
        for page_num in pages_to_process:
            page = doc[page_num]
            pix = page.get_pixmap(dpi=200)
            img_path = f"{base_name}_page_{page_num+1}.png"
            pix.save(img_path)
            
            with Image.open(img_path) as img:
                if print_type == "bw":
                    img = ImageOps.grayscale(img)
                single_path = f"{base_name}_single_{page_num+1}.jpg"
                img.save(single_path, "JPEG", quality=95)
                extracted_page_paths.append(single_path)
            
            if os.path.exists(img_path):
                os.remove(img_path)
        doc.close()

        if layout_mode == "2up" and len(extracted_page_paths) > 0:
            i = 0
            while i < len(extracted_page_paths):
                img1 = Image.open(extracted_page_paths[i])
                img2 = Image.open(extracted_page_paths[i+1]) if (i + 1) < len(extracted_page_paths) else None

                if img2:
                    w1, h1 = img1.size
                    w2, h2 = img2.size
                    max_w = max(w1, w2)
                    total_h = h1 + h2 + 60

                    combined_img = Image.new("RGB", (max_w, total_h), (255, 255, 255))
                    combined_img.paste(img1, ((max_w - w1) // 2, 0))
                    combined_img.paste(img2, ((max_w - w2) // 2, h1 + 60))

                    if print_type == "bw":
                        combined_img = ImageOps.grayscale(combined_img)

                    combined_path = f"{base_name}_2up_ready_{i//2 + 1}.jpg"
                    combined_img.save(combined_path, "JPEG", quality=95)
                    output_images.append(combined_path)
                    
                    img1.close()
                    img2.close()
                    combined_img.close()
                    i += 2
                else:
                    output_images.append(extracted_page_paths[i])
                    img1.close()
                    i += 1
            
            for p in extracted_page_paths:
                if os.path.exists(p):
                    os.remove(p)
        else:
            output_images = extracted_page_paths
    else:
        with Image.open(file_path) as img:
            if print_type == "bw":
                img = ImageOps.grayscale(img)
            final_path = f"{base_name}_ready.jpg"
            img.save(final_path, "JPEG", quality=95)
            output_images.append(final_path)

    return output_images

@app.get("/", response_class=HTMLResponse)
def home():
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Vicky Smart Kiosk - Professional Design</title>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.5.13/cropper.min.css"/>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.5.13/cropper.min.js"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/2.16.105/pdf.min.js"></script>
        <script>
            pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/2.16.105/pdf.worker.min.js';
        </script>
        
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background: linear-gradient(135deg, #1e1e2e 0%, #0f0f1e 100%);
                min-height: 100vh;
                display: flex;
                justify-content: center;
                align-items: center;
                padding: 20px;
                color: white;
            }
            .container {
                width: 100%;
                max-width: 600px;
                background: linear-gradient(135deg, #2d2d3d 0%, #1a1a2e 100%);
                border-radius: 20px;
                box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5), inset 0 1px 0 rgba(255, 255, 255, 0.1);
                padding: 30px;
                border: 1px solid rgba(255, 255, 255, 0.1);
            }
            .header { text-align: center; margin-bottom: 25px; }
            .logo { display: flex; align-items: center; justify-content: center; gap: 12px; margin-bottom: 10px; }
            .logo-icon {
                width: 40px; height: 40px;
                background: linear-gradient(135deg, #00d4ff 0%, #0099cc 100%);
                border-radius: 10px; display: flex; align-items: center; justify-content: center;
                font-size: 22px; color: white; box-shadow: 0 8px 20px rgba(0, 212, 255, 0.3);
            }
            .logo h1 { font-size: 24px; color: white; font-weight: 600; letter-spacing: 0.5px; }
            .subtitle { color: #00d4ff; font-size: 11px; margin-top: 4px; letter-spacing: 1px; text-transform: uppercase; }
            
            .status-section {
                background: rgba(0, 212, 255, 0.05);
                border: 1px solid rgba(0, 212, 255, 0.2);
                border-radius: 12px; padding: 14px; margin-bottom: 20px;
                display: flex; justify-content: space-between; align-items: center;
            }
            .status-info { display: flex; align-items: center; gap: 12px; }
            .status-dot {
                width: 10px; height: 10px; background: #00ff88; border-radius: 50%;
                box-shadow: 0 0 10px rgba(0, 255, 136, 0.6); animation: pulse 2s infinite;
            }
            @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
            .status-text { color: white; font-size: 13px; font-weight: 500; }
            .shop-id { color: #00d4ff; font-size: 11px; opacity: 0.8; }

            .tab-buttons { display: flex; gap: 10px; margin-bottom: 15px; }
            .tab-btn {
                flex: 1; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
                padding: 10px; border-radius: 10px; font-size: 12px; font-weight: 600; color: #aaa; cursor: pointer; text-align: center;
            }
            .tab-btn.active { background: rgba(0,212,255,0.15); border-color: #00d4ff; color: #00d4ff; }

            .tab-content { display: none; }
            .tab-content.active { display: block; }

            .file-box {
                border: 2px dashed rgba(0, 212, 255, 0.4); border-radius: 12px;
                background: rgba(0, 212, 255, 0.03); padding: 20px; text-align: center;
                cursor: pointer; transition: 0.3s; margin-bottom: 15px;
            }
            .file-box:hover { background: rgba(0, 212, 255, 0.08); border-color: #00d4ff; }
            .file-box input { display: none; }
            .file-box label { font-size: 13px; font-weight: 600; color: #00d4ff; cursor: pointer; display: block; }

            #crop-wrapper { display: none; margin-bottom: 15px; background: #000; border-radius: 10px; overflow: hidden; max-height: 350px; }
            #image-to-crop { max-width: 100%; display: block; }
            
            .crop-tools { display: none; grid-template-columns: repeat(2, 1fr); gap: 8px; margin-bottom: 15px; }
            .tool-btn { background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.15); padding: 8px; border-radius: 8px; font-size: 12px; font-weight: 600; cursor: pointer; color: white; transition: 0.2s; }
            .tool-btn:hover { background: rgba(255,255,255,0.15); }
            .crop-apply-btn { background: #00ff88; color: #000; border: none; grid-column: span 2; font-weight: 700; }
            .crop-apply-btn:hover { background: #00cc6a; }
            .aadhaar-btn { background: #ec4899; color: #fff; border: none; grid-column: span 2; font-weight: 700; }
            .aadhaar-btn:hover { background: #db2777; }
            .preview-btn { background: #38bdf8; color: #000; border: none; grid-column: span 1; font-weight: 700; }
            .preview-btn:hover { background: #0ea5e9; }
            .pc-open-btn { background: #f59e0b; color: #000; border: none; grid-column: span 1; font-weight: 700; }
            .pc-open-btn:hover { background: #d97706; }

            .brightness-box { display: none; background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.1); border-radius: 10px; padding: 12px; margin-bottom: 15px; }
            .brightness-box label { font-size: 12px; font-weight: 600; color: #aaa; display: flex; justify-content: space-between; }
            .brightness-slider { width: 100%; margin-top: 6px; cursor: pointer; accent-color: #00d4ff; }

            .pdf-options-box { display: none; background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.1); border-radius: 10px; padding: 12px; margin-bottom: 15px; }
            
            .options-card { background: rgba(255, 255, 255, 0.03); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 12px; padding: 15px; margin-bottom: 15px; }
            .section-label { color: #00d4ff; font-size: 11px; text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 8px; display: block; font-weight: 600; }
            
            .radio-group { display: flex; gap: 10px; margin-top: 6px; }
            .radio-label {
                flex: 1; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
                padding: 10px; border-radius: 8px; font-size: 12px; text-align: center; cursor: pointer;
                display: flex; align-items: center; justify-content: center; font-weight: 600; color: white;
            }
            .radio-label input { margin-right: 6px; accent-color: #00d4ff; }
            
            .input-field, .textarea-field {
                width: 100%; padding: 10px; background: rgba(0,0,0,0.2); border: 1px solid rgba(255,255,255,0.15);
                border-radius: 8px; font-size: 13px; margin-top: 4px; outline: none; color: white; font-family: 'Segoe UI';
            }
            .textarea-field { resize: vertical; height: 120px; }
            .input-field:focus, .textarea-field:focus { border-color: #00d4ff; }

            .bill-summary { background: rgba(0, 255, 136, 0.08); border: 1px solid rgba(0, 255, 136, 0.2); border-radius: 10px; padding: 12px; margin-bottom: 15px; text-align: center; }
            .bill-summary .amount { font-size: 20px; font-weight: 700; color: #00ff88; }

            .submit-btn {
                width: 100%; background: linear-gradient(135deg, #00d4ff 0%, #0099cc 100%); color: white; border: none;
                padding: 12px; border-radius: 10px; font-size: 14px; font-weight: 700; cursor: pointer;
                text-transform: uppercase; letter-spacing: 0.5px; box-shadow: 0 8px 20px rgba(0, 212, 255, 0.3); transition: 0.3s;
            }
            .submit-btn:hover { transform: translateY(-2px); box-shadow: 0 12px 25px rgba(0, 212, 255, 0.4); }

            #status { display: none; margin-top: 15px; padding: 12px; border-radius: 10px; font-size: 12px; line-height: 1.4; font-weight: 600; }
            .status-success { background: rgba(0,255,136,0.1); color: #00ff88; border: 1px solid rgba(0,255,136,0.3); }
            .status-error { background: rgba(255,71,87,0.1); color: #ff4757; border: 1px solid rgba(255,71,87,0.3); }
            .status-info { background: rgba(0,212,255,0.1); color: #00d4ff; border: 1px solid rgba(0,212,255,0.3); }

            #previewModal {
                display: none; position: fixed; z-index: 9999; left: 0; top: 0; width: 100%; height: 100%;
                background-color: rgba(0,0,0,0.85); justify-content: center; align-items: center; padding: 20px;
            }
            .modal-content {
                background: #1e293b; border-radius: 12px; max-width: 500px; width: 100%; padding: 20px;
                border: 1px solid rgba(255,255,255,0.15); text-align: center; position: relative;
            }
            .modal-content img { max-width: 100%; max-height: 400px; border-radius: 8px; margin-top: 10px; border: 1px solid rgba(255,255,255,0.1); }
            .close-modal {
                background: #ef4444; color: white; border: none; padding: 8px 16px; border-radius: 6px;
                font-weight: 700; cursor: pointer; margin-top: 15px;
            }

            .footer-brand { text-align: center; font-size: 10px; color: #777; margin-top: 20px; border-top: 1px solid rgba(255,255,255,0.05); padding-top: 10px; }
        </style>
    </head>
    <body>

    <div class="container">
        <div class="header">
            <div class="logo">
                <div class="logo-icon">⚡</div>
                <h1>Vicky Smart Kiosk</h1>
            </div>
            <p class="subtitle">Secure Enterprise Cloud Printing Portal</p>
        </div>

        <div class="status-section">
            <div class="status-info">
                <div class="status-dot"></div>
                <div>
                    <div class="status-text">System Status</div>
                    <div class="shop-id">Cloud Gateway Active</div>
                </div>
            </div>
            <div class="status-text" style="color: #00ff88;">ONLINE & LIVE</div>
        </div>

        <!-- Mode Selection Tabs -->
        <div class="tab-buttons">
            <div class="tab-btn active" id="tabFileBtn" onclick="switchMode('file')">📁 File / Aadhaar / PDF</div>
            <div class="tab-btn" id="tabTextBtn" onclick="switchMode('text')">✏️ Type & Print Text</div>
        </div>

        <form id="printForm">
            <!-- FILE / PHOTO / AADHAAR MODE -->
            <div id="fileModeSection" class="tab-content active">
                
                <!-- Dedicated Aadhaar Card Quick Mode Selector -->
                <div class="options-card" style="border-color: #ec4899; background: rgba(236,72,153,0.03);">
                    <span class="section-label" style="color: #ec4899;">🆔 Aadhaar Card / ID Card Print Mode (Front & Back)</span>
                    <p style="font-size: 11px; color: #aaa; margin-bottom: 8px;">Aadhaar card ke liye dono side ek hi page par print karne ke liye yahan select karein:</p>
                    <div class="file-box" style="border-color: #ec4899; margin-bottom: 10px;" onclick="document.getElementById('aadhaarFrontInput').click()">
                        <label id="aadhaarFrontLabel" style="color: #ec4899;">1️⃣ Select Aadhaar Front Photo (Aage ki photo)</label>
                        <input type="file" id="aadhaarFrontInput" accept=".png,.jpg,.jpeg,.heic" onchange="handleAadhaarSelect('front', event)">
                    </div>
                    <div class="file-box" style="border-color: #ec4899; margin-bottom: 0;" onclick="document.getElementById('aadhaarBackInput').click()">
                        <label id="aadhaarBackLabel" style="color: #ec4899;">2️⃣ Select Aadhaar Back Photo (Peeche ki photo)</label>
                        <input type="file" id="aadhaarBackInput" accept=".png,.jpg,.jpeg,.heic" onchange="handleAadhaarSelect('back', event)">
                    </div>
                </div>

                <div style="text-align: center; margin: 10px 0; color: #777; font-size: 11px;">--- YA NORMAL DOCUMENT / PDF CHOOSE KAREIN ---</div>

                <span class="section-label">📁 General Document / Multiple Photos</span>
                <div class="file-box" onclick="document.getElementById('fileInput').click()">
                    <label id="fileLabel">📸 Photo / PDF Select Karein (Multiple Supported)</label>
                    <input type="file" id="fileInput" accept=".pdf,.png,.jpg,.jpeg,.heic" multiple onchange="handleFileSelect(event)">
                </div>

                <!-- NEW: Multiple Photos Layout Option Container -->
                <div class="pdf-options-box" id="multipleLayoutBox" style="display: none;">
                    <label style="font-size: 11px; color: #aaa;">Multiple Photos Page Layout (Bachat Option):</label>
                    <div class="radio-group">
                        <label class="radio-label">
                            <input type="radio" name="multiLayoutMode" value="normal" checked onchange="updateBill()">
                            📄 Normal (1 Photo/Sheet)
                        </label>
                        <label class="radio-label">
                            <input type="radio" name="multiLayoutMode" value="2up" onchange="updateBill()">
                            📑 2-in-1 (2 Photos/Sheet)
                        </label>
                    </div>
                </div>

                <div id="crop-wrapper">
                    <img id="image-to-crop" alt="Selected Image">
                </div>

                <div class="crop-tools" id="cropTools">
                    <button type="button" class="tool-btn crop-apply-btn" onclick="applyCrop()">✂️ Apply Crop (Selective Photo)</button>
                    <button type="button" class="tool-btn preview-btn" onclick="openPrintPreview()">👁️ Print Preview (Dekhein)</button>
                    <button type="button" class="tool-btn pc-open-btn" onclick="openOnComputer()">💻 Open on PC & Print</button>
                    <button type="button" class="tool-btn" onclick="rotateImage(-90)">↺ Rotate Left</button>
                    <button type="button" class="tool-btn" onclick="rotateImage(90)">↻ Rotate Right</button>
                    <button type="button" class="tool-btn" onclick="clearCropArea()">📐 Full Image</button>
                    <button type="button" class="tool-btn" onclick="resetCrop()">🔄 Reset</button>
                </div>

                <div class="brightness-box" id="brightnessBox">
                    <label for="brightnessRange">
                        <span>☀️ Brightness (Ujala):</span>
                        <span id="brightnessVal">100%</span>
                    </label>
                    <input type="range" id="brightnessRange" class="brightness-slider" min="50" max="200" value="100" oninput="adjustBrightness(this.value)">
                </div>

                <div class="pdf-options-box" id="pdfOptionsBox">
                    <span class="section-label" style="margin-bottom:4px;">📄 Specific Page Number</span>
                    <input type="number" class="input-field" id="pageInput" placeholder="Khali chhodein agar sabhi pages chahiye" min="1" oninput="updateBill()">
                    
                    <div style="margin-top: 12px;">
                        <label style="font-size: 11px; color: #aaa;">PDF Page Layout (Bachat Option):</label>
                        <div class="radio-group">
                            <label class="radio-label">
                                <input type="radio" name="layoutMode" value="normal" checked onchange="updateBill()">
                                📄 Normal (1 Page/Sheet)
                            </label>
                            <label class="radio-label">
                                <input type="radio" name="layoutMode" value="2up" onchange="updateBill()">
                                📑 2-in-1 (2 Pages/Sheet)
                            </label>
                        </div>
                    </div>
                </div>
            </div>

            <!-- TEXT WRITING & EDITING MODE -->
            <div id="textModeSection" class="tab-content">
                <span class="section-label">✏️ Kuchh Bhi Likhein aur Print Karwayein</span>
                <textarea id="textContent" class="textarea-field" spellcheck="false" placeholder="Yahan apna text ya application type karein..." oninput="updateBill()"></textarea>
                
                <!-- Text Size Selector Option -->
                <div style="margin-top: 12px;">
                    <label style="font-size: 11px; color: #aaa;">Text Font Size ( अक्षर साइज़ ):</label>
                    <select id="textSizeSelect" class="input-field" onchange="updateBill()">
                        <option value="24">Small (24px)</option>
                        <option value="32" selected>Normal / Standard (32px)</option>
                        <option value="40">Large (40px)</option>
                        <option value="48">Extra Large (48px)</option>
                    </select>
                </div>
            </div>

            <!-- ADVANCED PAGE SETUP OPTIONS -->
            <div class="options-card">
                <span class="section-label">⚙️ Page Setup & Formatting</span>
                
                <div style="margin-bottom: 12px;">
                    <label style="font-size: 11px; color: #aaa;">Orientation (पेज की दिशा):</label>
                    <div class="radio-group">
                        <label class="radio-label">
                            <input type="radio" name="orientation" value="portrait" checked>
                            📜 Portrait (खड़ा)
                        </label>
                        <label class="radio-label">
                            <input type="radio" name="orientation" value="landscape">
                            📄 Landscape (आड़ा)
                        </label>
                    </div>
                </div>

                <div style="margin-bottom: 12px;">
                    <label style="font-size: 11px; color: #aaa;">Paper Size (पेपर साइज़):</label>
                    <select id="paperSize" class="input-field">
                        <option value="A4" selected>A4 Standard</option>
                        <option value="Letter">Letter Size</option>
                        <option value="Legal">Legal Size</option>
                    </select>
                </div>

                <div>
                    <label style="font-size: 11px; color: #aaa;">Margin & Border (बॉर्डर और मार्जिन):</label>
                    <div class="radio-group">
                        <label class="radio-label">
                            <input type="radio" name="marginMode" value="standard" checked>
                            📐 Standard Margin
                        </label>
                        <label class="radio-label">
                            <input type="radio" name="marginMode" value="bordered">
                            🖼️ With Box Border
                        </label>
                    </div>
                </div>
            </div>

            <!-- GENERAL PRINT SETTINGS -->
            <div class="options-card">
                <span class="section-label">🖨️ Print Settings</span>
                <div style="margin-bottom: 12px;">
                    <label style="font-size: 11px; color: #aaa;">Print Type:</label>
                    <div class="radio-group">
                        <label class="radio-label">
                            <input type="radio" name="printType" value="bw" checked onchange="updateBill()">
                            ⚪ B&W (₹3)
                        </label>
                        <label class="radio-label">
                            <input type="radio" name="printType" value="color" onchange="updateBill()">
                            🎨 Color (₹10)
                        </label>
                    </div>
                </div>
                <div>
                    <label style="font-size: 11px; color: #aaa;">Copies Number:</label>
                    <input type="number" class="input-field" id="copies" value="1" min="1" max="100" oninput="updateBill()">
                </div>
            </div>

            <div class="bill-summary">
                <div style="font-size: 11px; color: #aaa;">Estimated Bill Amount:</div>
                <div class="amount" id="totalBillDisplay">₹3</div>
            </div>

            <button type="button" class="submit-btn" onclick="submitPrintJob()">⚡ Confirm & Print Now</button>
        </form>

        <div id="status"></div>
        <div class="footer-brand">© Vicky Smart Kiosk Enterprise • Developed by Vicky Kushwaha</div>
    </div>

    <div id="previewModal">
        <div class="modal-content">
            <h3 style="color: #00d4ff; font-size: 16px; margin-bottom: 5px;">🖨️ Print Preview Window</h3>
            <p style="font-size: 11px; color: #aaa;">Aisa print paper par aayega</p>
            <img id="previewImageModal" src="" alt="Print Preview">
            <br>
            <button class="close-modal" onclick="closePrintPreview()">❌ Band Karein</button>
        </div>
    </div>

    <script>
        let cropper = null;
        let isImage = false;
        let currentBrightness = 100;
        let totalPdfPages = 1;
        let currentMode = 'file';
        let selectedFilesList = [];
        let aadhaarFrontFile = null;
        let aadhaarBackFile = null;

        function switchMode(mode) {
            currentMode = mode;
            document.getElementById('tabFileBtn').classList.toggle('active', mode === 'file');
            document.getElementById('tabTextBtn').classList.toggle('active', mode === 'text');
            document.getElementById('fileModeSection').classList.toggle('active', mode === 'file');
            document.getElementById('textModeSection').classList.toggle('active', mode === 'text');
            updateBill();
        }

        function handleAadhaarSelect(side, event) {
            const file = event.target.files[0];
            if (!file) return;

            if (side === 'front') {
                aadhaarFrontFile = file;
                document.getElementById('aadhaarFrontLabel').innerText = `✅ Front Selected: ${file.name}`;
            } else {
                aadhaarBackFile = file;
                document.getElementById('aadhaarBackLabel').innerText = `✅ Back Selected: ${file.name}`;
            }
            selectedFilesList = [];
            document.getElementById('multipleLayoutBox').style.display = 'none';
            updateBill();
        }

        async function handleFileSelect(event) {
            const files = event.target.files;
            if (!files || files.length === 0) return;

            selectedFilesList = Array.from(files);
            aadhaarFrontFile = null;
            aadhaarBackFile = null;
            document.getElementById('aadhaarFrontLabel').innerText = `1️⃣ Select Aadhaar Front Photo (Aage ki photo)`;
            document.getElementById('aadhaarBackLabel').innerText = `2️⃣ Select Aadhaar Back Photo (Peeche ki photo)`;

            const cropWrapper = document.getElementById('crop-wrapper');
            const cropTools = document.getElementById('cropTools');
            const brightnessBox = document.getElementById('brightnessBox');
            const pdfOptionsBox = document.getElementById('pdfOptionsBox');
            const multipleLayoutBox = document.getElementById('multipleLayoutBox');

            if (cropper) {
                cropper.destroy();
                cropper = null;
            }

            document.getElementById('pageInput').value = "";
            document.getElementById('brightnessRange').value = 100;
            document.getElementById('brightnessVal').innerText = "100%";
            currentBrightness = 100;

            const file = selectedFilesList[0];

            if (file.type.includes('image') || file.name.toLowerCase().endsWith('.heic')) {
                isImage = true;
                totalPdfPages = selectedFilesList.length;
                if(selectedFilesList.length > 1) {
                    document.getElementById('fileLabel').innerText = `📄 Multiple Photos Selected: ${selectedFilesList.length} Files`;
                    multipleLayoutBox.style.display = 'block';
                } else {
                    document.getElementById('fileLabel').innerText = `📄 Selected: ${file.name} (1 Page)`;
                    multipleLayoutBox.style.display = 'none';
                }
                
                cropWrapper.style.display = selectedFilesList.length === 1 ? 'block' : 'none';
                cropTools.style.display = selectedFilesList.length === 1 ? 'grid' : 'none';
                brightnessBox.style.display = selectedFilesList.length === 1 ? 'block' : 'none';
                pdfOptionsBox.style.display = 'none';

                if (selectedFilesList.length === 1) {
                    const reader = new FileReader();
                    reader.onload = function(e) {
                        const imgElement = document.getElementById('image-to-crop');
                        imgElement.src = e.target.result;
                        imgElement.style.filter = 'brightness(100%)';

                        cropper = new Cropper(imgElement, {
                            viewMode: 1,
                            autoCropArea: 1.0,
                            responsive: true,
                            background: false
                        });
                    };
                    reader.readAsDataURL(file);
                }
                updateBill();
            } else {
                isImage = false;
                multipleLayoutBox.style.display = 'none';
                cropWrapper.style.display = 'none';
                cropTools.style.display = 'none';
                brightnessBox.style.display = 'none';
                pdfOptionsBox.style.display = 'block';

                document.getElementById('fileLabel').innerText = `⏳ Reading PDF: ${file.name}...`;

                try {
                    const arrayBuffer = await file.arrayBuffer();
                    const loadingTask = pdfjsLib.getDocument({ data: arrayBuffer });
                    const pdfDoc = await loadingTask.promise;
                    
                    totalPdfPages = pdfDoc.numPages;
                    document.getElementById('fileLabel').innerText = `📄 Selected: ${file.name} (${totalPdfPages} Pages)`;
                } catch (err) {
                    totalPdfPages = 1;
                    document.getElementById('fileLabel').innerText = `📄 Selected: ${file.name}`;
                }
                updateBill();
            }
        }

        function adjustBrightness(val) {
            currentBrightness = val;
            document.getElementById('brightnessVal').innerText = val + "%";
            const cropperCanvas = document.querySelector('.cropper-container img');
            if (cropperCanvas) {
                cropperCanvas.style.filter = `brightness(${val}%)`;
            }
        }

        function rotateImage(deg) { if (cropper) cropper.rotate(deg); }
        function resetCrop() { if (cropper) cropper.reset(); }
        
        function clearCropArea() { 
            if (cropper) { 
                cropper.setCropBoxData({
                    left: 0, top: 0,
                    width: cropper.getContainerData().width,
                    height: cropper.getContainerData().height
                });
            } 
        }

        function applyCrop() {
            if (!cropper) return;
            const canvas = getCanvasWithBrightness();

            if (canvas) {
                const croppedDataUrl = canvas.toDataURL('image/jpeg', 0.95);
                cropper.destroy();
                cropper = null;

                const imgElement = document.getElementById('image-to-crop');
                imgElement.src = croppedDataUrl;
                imgElement.style.filter = 'brightness(100%)';

                document.getElementById('brightnessRange').value = 100;
                document.getElementById('brightnessVal').innerText = "100%";
                currentBrightness = 100;

                cropper = new Cropper(imgElement, {
                    viewMode: 1, autoCropArea: 1, responsive: true, background: false
                });
            }
        }

        function openPrintPreview() {
            if (!isImage || !cropper) {
                alert("Kripya pehle image select karein aur crop/adjust karein!");
                return;
            }
            const canvas = getCanvasWithBrightness();
            if (canvas) {
                const dataUrl = canvas.toDataURL('image/jpeg', 0.95);
                document.getElementById('previewImageModal').src = dataUrl;
                document.getElementById('previewModal').style.display = 'flex';
            }
        }

        function closePrintPreview() {
            document.getElementById('previewModal').style.display = 'none';
        }

        async function openOnComputer() {
            const statusBox = document.getElementById('status');
            if (currentMode === 'text') {
                alert('Text mode mein ye option available nahi hai!');
                return;
            }
            if (selectedFilesList.length === 0 && !aadhaarFrontFile) {
                alert('Kripya pehle koi Photo ya Aadhaar select karein!');
                return;
            }

            statusBox.style.display = 'block';
            statusBox.className = 'status-info';
            statusBox.innerText = '💻 Computer screen par file khol rahe hain...';

            const formData = new FormData();
            const printType = document.querySelector('input[name="printType"]:checked').value;
            formData.append('print_type', printType);

            if (aadhaarFrontFile && aadhaarBackFile) {
                formData.append('print_mode', 'aadhaar_card');
                formData.append('front_file', aadhaarFrontFile);
                formData.append('back_file', aadhaarBackFile);
                await sendOpenOnPCRequest(formData);
            } else if (isImage && cropper && selectedFilesList.length === 1) {
                const canvas = getCanvasWithBrightness();
                canvas.toBlob(async (blob) => {
                    formData.append('file', blob, 'pc_view_doc.jpg');
                    await sendOpenOnPCRequest(formData);
                }, 'image/jpeg', 0.95);
            } else {
                formData.append('file', selectedFilesList[0]);
                await sendOpenOnPCRequest(formData);
            }
        }

        async function sendOpenOnPCRequest(formData) {
            const statusBox = document.getElementById('status');
            try {
                const response = await fetch('/open-on-pc', { method: 'POST', body: formData });
                const result = await response.json();
                if (response.ok) {
                    statusBox.className = 'status-success';
                    statusBox.innerText = '✅ File computer screen par khol di gayi hai!';
                } else {
                    statusBox.className = 'status-error';
                    statusBox.innerText = '❌ Error: ' + (result.detail || 'Failed to open on PC.');
                }
            } catch (err) {
                statusBox.className = 'status-error';
                statusBox.innerText = '❌ Network Server Error!';
            }
        }

        function getCanvasWithBrightness() {
            if (!cropper) return null;
            const baseCanvas = cropper.getCroppedCanvas({
                maxWidth: 2480, maxHeight: 3508, fillColor: '#fff'
            });
            if (!baseCanvas) return null;

            const finalCanvas = document.createElement('canvas');
            finalCanvas.width = baseCanvas.width;
            finalCanvas.height = baseCanvas.height;
            const ctx = finalCanvas.getContext('2d');

            ctx.filter = `brightness(${currentBrightness}%)`;
            ctx.drawImage(baseCanvas, 0, 0);

            return finalCanvas;
        }

        function updateBill() {
            const type = document.querySelector('input[name="printType"]:checked').value;
            const rate = type === 'color' ? 10 : 3;
            const copies = parseInt(document.getElementById('copies').value) || 1;
            
            let pagesToBill = 1;
            if (currentMode === 'text') {
                const textContent = document.getElementById('textContent').value;
                if (textContent.trim() !== "") {
                    pagesToBill = Math.max(1, Math.ceil(textContent.length / 800));
                } else {
                    pagesToBill = 1;
                }
            } else if (aadhaarFrontFile && aadhaarBackFile) {
                pagesToBill = 1;
            } else {
                if (selectedFilesList.length > 1) {
                    const multiLayout = document.querySelector('input[name="multiLayoutMode"]:checked') ? document.querySelector('input[name="multiLayoutMode"]:checked').value : 'normal';
                    if (multiLayout === '2up') {
                        pagesToBill = Math.ceil(selectedFilesList.length / 2);
                    } else {
                        pagesToBill = selectedFilesList.length;
                    }
                } else {
                    const pageInput = document.getElementById('pageInput');
                    const layoutMode = document.querySelector('input[name="layoutMode"]:checked') ? document.querySelector('input[name="layoutMode"]:checked').value : 'normal';

                    if (isImage) {
                        pagesToBill = 1;
                    } else {
                        if (pageInput && pageInput.value.trim() !== "") {
                            pagesToBill = 1;
                        } else {
                            pagesToBill = totalPdfPages;
                            if (layoutMode === '2up') {
                                pagesToBill = Math.ceil(totalPdfPages / 2);
                            }
                        }
                    }
                }
            }
            
            document.getElementById('totalBillDisplay').innerText = "₹" + (rate * copies * pagesToBill);
        }

        async function submitPrintJob() {
            const statusBox = document.getElementById('status');
            const printType = document.querySelector('input[name="printType"]:checked').value;
            const copies = document.getElementById('copies').value;
            const orientation = document.querySelector('input[name="orientation"]:checked').value;
            const paperSize = document.getElementById('paperSize').value;
            const marginMode = document.querySelector('input[name="marginMode"]:checked').value;

            statusBox.style.display = 'block';
            statusBox.className = 'status-info';
            statusBox.innerText = '⚡ Processing & Uploading...';

            const formData = new FormData();
            formData.append('print_type', printType);
            formData.append('copies', copies);
            formData.append('orientation', orientation);
            formData.append('paper_size', paperSize);
            formData.append('margin_mode', marginMode);

            if (currentMode === 'text') {
                const textContent = document.getElementById('textContent').value.trim();
                const textSize = document.getElementById('textSizeSelect').value;
                if (!textContent) {
                    alert('Kripya print karne ke liye kuch text type karein!');
                    statusBox.style.display = 'none';
                    return;
                }
                formData.append('print_mode', 'text');
                formData.append('text_content', textContent);
                formData.append('text_size', textSize);
                await sendToServer(formData);
            } else if (aadhaarFrontFile && aadhaarBackFile) {
                formData.append('print_mode', 'aadhaar_card');
                formData.append('front_file', aadhaarFrontFile);
                formData.append('back_file', aadhaarBackFile);
                await sendToServer(formData);
            } else {
                if (selectedFilesList.length === 0) {
                    alert('Kripya pehle koi Photo ya PDF file choose karein!');
                    statusBox.style.display = 'none';
                    return;
                }

                if (selectedFilesList.length > 1) {
                    const multiLayout = document.querySelector('input[name="multiLayoutMode"]:checked') ? document.querySelector('input[name="multiLayoutMode"]:checked').value : 'normal';
                    formData.append('print_mode', 'multiple_files');
                    formData.append('layout_mode', multiLayout);
                    for (let i = 0; i < selectedFilesList.length; i++) {
                        formData.append('files', selectedFilesList[i]);
                    }
                    await sendToServer(formData);
                } else {
                    const pageInput = document.getElementById('pageInput').value;
                    const layoutMode = document.querySelector('input[name="layoutMode"]:checked') ? document.querySelector('input[name="layoutMode"]:checked').value : 'normal';

                    formData.append('print_mode', 'file');
                    formData.append('page_selection', pageInput);
                    formData.append('layout_mode', layoutMode);

                    if (isImage && cropper) {
                        const canvas = getCanvasWithBrightness();
                        canvas.toBlob(async (blob) => {
                            formData.append('file', blob, 'processed_doc.jpg');
                            await sendToServer(formData);
                        }, 'image/jpeg', 0.95);
                    } else {
                        formData.append('file', selectedFilesList[0]);
                        await sendToServer(formData);
                    }
                }
            }
        }

        async function sendToServer(formData) {
            const statusBox = document.getElementById('status');
            try {
                const response = await fetch('/upload', { method: 'POST', body: formData });
                const result = await response.json();

                if (response.status === 202) {
                    statusBox.className = 'status-info';
                    statusBox.innerText = `⏳ Badi PDF file upload ho chuki hai (${result.total_pages} Pages). Dukandar (PC) se approval ka intezaار ho raha hai...`;
                    
                    const jobId = result.job_id;
                    pollApprovalStatus(jobId, statusBox);
                    return;
                }

                if (response.ok) {
                    statusBox.className = 'status-success';
                    statusBox.innerHTML = `
                        <strong>✅ Instant Print Executed!</strong><br>
                        🖨️ <b>Copies Printed:</b> ${result.copies}<br>
                        ⚪🎨 <b>Mode:</b> ${result.print_type.toUpperCase()}<br>
                        💰 <b>Total Bill: ₹${result.total_bill}</b>
                    `;
                } else {
                    statusBox.className = 'status-error';
                    statusBox.innerText = '❌ Error: ' + (result.detail || 'Print failed.');
                }
            } catch (err) {
                statusBox.className = 'status-error';
                statusBox.innerText = '❌ Network Server Error!';
            }
        }

        async function pollApprovalStatus(jobId, statusBox) {
            const interval = setInterval(async () => {
                try {
                    const res = await fetch(`/check-approval/${jobId}`);
                    const data = await res.json();

                    if (data.status === 'approved') {
                        clearInterval(interval);
                        statusBox.className = 'status-success';
                        statusBox.innerHTML = `
                            <strong>✅ Dukandar ne approve kar diya! Print execute ho raha hai.</strong><br>
                            🖨️ <b>Copies:</b> ${data.copies}<br>
                            💰 <b>Total Bill: ₹${data.total_bill}</b>
                        `;
                    } else if (data.status === 'rejected') {
                        clearInterval(interval);
                        statusBox.className = 'status-error';
                        statusBox.innerText = '❌ Dukandar ne is print job ko reject kar diya hai.';
                    }
                } catch (e) {}
            }, 2000);
        }
    </script>
    </body>
    </html>
    """
    return html_content

@app.post("/open-on-pc")
async def open_on_pc(
    request: Request,
    print_type: str = Form("bw"),
    file: UploadFile = File(None),
    front_file: UploadFile = File(None),
    back_file: UploadFile = File(None)
):
    upload_folder = "uploads"
    os.makedirs(upload_folder, exist_ok=True)
    
    form_data = await request.form()
    print_mode = form_data.get("print_mode", "file")

    if print_mode == "aadhaar_card" and front_file and back_file:
        f_path = os.path.join(upload_folder, f"front_{front_file.filename}")
        b_path = os.path.join(upload_folder, f"back_{back_file.filename}")
        with open(f_path, "wb") as f:
            f.write(await front_file.read())
        with open(b_path, "wb") as f:
            f.write(await back_file.read())
        
        printable_images = process_aadhaar_card_images(f_path, b_path, print_type)
        target_file = printable_images[0]
    else:
        file_path = os.path.join(upload_folder, file.filename)
        with open(file_path, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        printable_images = process_file_to_printable_images(file_path, print_type)
        target_file = printable_images[0] if printable_images else file_path

    try:
        os.startfile(os.path.abspath(target_file))
    except Exception as e:
        raise Exception(f"PC par file kholne me error aayi: {e}")

    return {"status": "success", "message": "Opened on PC successfully"}

def process_aadhaar_card_images(front_path, back_path, print_type):
    upload_folder = "uploads"
    img1 = Image.open(front_path)
    img2 = Image.open(back_path)

    a4_w, a4_h = 1654, 2339
    canvas = Image.new("RGB", (a4_w, a4_h), (255, 255, 255))

    card_w = 1200
    w1, h1 = img1.size
    h1_new = int((card_w / w1) * h1)
    img1_resized = img1.resize((card_w, h1_new), Image.Resampling.LANCZOS)

    w2, h2 = img2.size
    h2_new = int((card_w / w2) * h2)
    img2_resized = img2.resize((card_w, h2_new), Image.Resampling.LANCZOS)

    x_offset = (a4_w - card_w) // 2
    y_offset_1 = 200
    y_offset_2 = y_offset_1 + h1_new + 150

    canvas.paste(img1_resized, (x_offset, y_offset_1))
    canvas.paste(img2_resized, (x_offset, y_offset_2))

    if print_type == "bw":
        canvas = ImageOps.grayscale(canvas)

    combined_path = os.path.join(upload_folder, f"aadhaar_ready_{int(time.time())}.jpg")
    canvas.save(combined_path, "JPEG", quality=95)
    
    img1.close()
    img2.close()
    canvas.close()
    return [combined_path]

@app.post("/upload")
async def upload_document(
    request: Request,
    print_mode: str = Form("file"),
    text_content: str = Form(""),
    text_size: int = Form(32),
    file: UploadFile = File(None),
    front_file: UploadFile = File(None),
    back_file: UploadFile = File(None),
    print_type: str = Form("bw"),
    copies: int = Form(1),
    page_selection: str = Form(""),
    layout_mode: str = Form("normal"),
    orientation: str = Form("portrait"),
    paper_size: str = Form("A4"),
    margin_mode: str = Form("standard")
):
    global TODAY_PRINT_COUNT
    upload_folder = "uploads"
    os.makedirs(upload_folder, exist_ok=True)

    form_data = await request.form()
    uploaded_files = form_data.getlist("files")

    if print_mode == "aadhaar_card" and front_file and back_file:
        f_path = os.path.join(upload_folder, f"front_{front_file.filename}")
        b_path = os.path.join(upload_folder, f"back_{back_file.filename}")
        with open(f_path, "wb") as f:
            f.write(await front_file.read())
        with open(b_path, "wb") as f:
            f.write(await back_file.read())

        printable_images = process_aadhaar_card_images(f_path, b_path, print_type)
        return await execute_print_job("Aadhaar_Card_Front_Back.jpg", printable_images[0], print_type, copies, is_preprocessed_image=True)

    if print_mode == "multiple_files" and uploaded_files:
        all_extracted_paths = []
        saved_file_paths = []
        total_files_count = len(uploaded_files)

        for ufile in uploaded_files:
            if ufile.filename:
                f_path = os.path.join(upload_folder, ufile.filename)
                with open(f_path, "wb") as f:
                    while True:
                        chunk = await ufile.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                saved_file_paths.append(f_path)
                with Image.open(f_path) as img:
                    if print_type == "bw":
                        img = ImageOps.grayscale(img)
                    single_path = f"{os.path.splitext(f_path)[0]}_single.jpg"
                    img.save(single_path, "JPEG", quality=95)
                    all_extracted_paths.append(single_path)

        all_printable_images = []
        if layout_mode == "2up" and len(all_extracted_paths) > 0:
            i = 0
            while i < len(all_extracted_paths):
                img1 = Image.open(all_extracted_paths[i])
                img2 = Image.open(all_extracted_paths[i+1]) if (i + 1) < len(all_extracted_paths) else None

                if img2:
                    w1, h1 = img1.size
                    w2, h2 = img2.size
                    max_w = max(w1, w2)
                    total_h = h1 + h2 + 60

                    combined_img = Image.new("RGB", (max_w, total_h), (255, 255, 255))
                    combined_img.paste(img1, ((max_w - w1) // 2, 0))
                    combined_img.paste(img2, ((max_w - w2) // 2, h1 + 60))

                    if print_type == "bw":
                        combined_img = ImageOps.grayscale(combined_img)

                    combined_path = os.path.join(upload_folder, f"multi_2up_ready_{int(time.time())}_{i//2}.jpg")
                    combined_img.save(combined_path, "JPEG", quality=95)
                    all_printable_images.append(combined_path)
                    
                    img1.close()
                    img2.close()
                    combined_img.close()
                    i += 2
                else:
                    all_printable_images.append(all_extracted_paths[i])
                    img1.close()
                    i += 1
            for p in all_extracted_paths:
                if os.path.exists(p):
                    os.remove(p)
        else:
            all_printable_images = all_extracted_paths

        rate = 10 if print_type == "color" else 3
        total_printed_pages = copies * len(all_printable_images)
        total_bill = rate * total_printed_pages
        TODAY_PRINT_COUNT += total_printed_pages

        try:
            if getattr(sys, 'frozen', False):
                base_path = os.path.dirname(sys.executable)
            else:
                base_path = os.path.dirname(os.path.abspath(__file__))

            sumatra_path = os.path.join(base_path, "SumatraPDF.exe")
            if not os.path.exists(sumatra_path):
                sumatra_path = os.path.join(base_path, "SumatraPDF.exe.exe")

            if os.path.exists(sumatra_path):
                print_queue.put((sumatra_path, all_printable_images, copies, SELECTED_PRINTER))
            else:
                print("[WARNING] SumatraPDF.exe folder mein nahi mila!")
        except Exception as e:
            raise Exception(f"Printer execution error: {e}")

        auto_delete_files(saved_file_paths + all_printable_images, delay=120)

        return {
            "filename": f"{total_files_count} Multiple Files",
            "copies": total_printed_pages,
            "print_type": print_type,
            "total_bill": total_bill
        }

    file_path = ""
    filename = ""

    if print_mode == "text":
        filename = f"text_print_{int(time.time())}.txt"
        file_path = os.path.join(upload_folder, filename)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(text_content)
        
        if paper_size == "Letter":
            w, h = (2550, 3300) if orientation == "portrait" else (3300, 2550)
        else:
            w, h = (2480, 3508) if orientation == "portrait" else (3508, 2480)
            
        img = Image.new("RGB", (w, h), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        
        if margin_mode == "bordered":
            draw.rectangle([60, 60, w - 60, h - 60], outline="black", width=5)
            
        # Robust Font Loader preventing square boxes / missing glyphs
        font = None
        for font_name in ["arial.ttf", "seguui.ttf", "calibri.ttf", "tahoma.ttf"]:
            try:
                font = ImageFont.truetype(font_name, int(text_size))
                break
            except:
                continue
        if font is None:
            font = ImageFont.load_default()
            
        margin = 120 if margin_mode == "bordered" else 100
        max_width_px = w - (margin * 2)
        
        paragraphs = text_content.split('\n')
        wrapped_lines = []
        for para in paragraphs:
            if para.strip() == "":
                wrapped_lines.append("")
                continue
            words = para.split(' ')
            current_line = ""
            for word in words:
                test_line = current_line + " " + word if current_line else word
                # Measure text width accurately using font.getlength if available
                try:
                    text_w = font.getlength(test_line)
                except:
                    text_w = len(test_line) * (int(text_size) * 0.55)
                
                if text_w < max_width_px:
                    current_line = test_line
                else:
                    wrapped_lines.append(current_line)
                    current_line = word
            if current_line:
                wrapped_lines.append(current_line)

        y_text = margin
        line_height = int(int(text_size) * 1.5)
        for line in wrapped_lines:
            draw.text((margin, y_text), line, fill="black", font=font)
            y_text += line_height
            if y_text > h - 150:
                break
                
        img_ready_path = os.path.join(upload_folder, f"text_ready_{int(time.time())}.jpg")
        if print_type == "bw":
            img = ImageOps.grayscale(img)
        img.save(img_ready_path, "JPEG", quality=95)
        return await execute_print_job("Typed_Document.jpg", img_ready_path, print_type, copies, is_preprocessed_image=True)
    else:
        if file is None or file.filename == "":
            raise HTTPException(status_code=400, detail="No file uploaded")
        filename = file.filename
        file_path = os.path.join(upload_folder, filename)
        with open(file_path, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)

    total_pages = 1
    is_pdf = filename.lower().endswith('.pdf')
    if is_pdf and fitz is not None:
        try:
            doc = fitz.open(file_path)
            total_pages = len(doc)
            doc.close()
        except:
            pass

    if is_pdf and total_pages > 100:
        job_id = str(random.randint(100000, 999999))
        PENDING_APPROVALS[job_id] = {
            "status": "pending",
            "filename": filename,
            "total_pages": total_pages,
            "file_path": file_path,
            "print_type": print_type,
            "copies": copies,
            "page_selection": page_selection,
            "layout_mode": layout_mode
        }
        return JSONResponse(status_code=202, content={
            "status": "pending_approval",
            "job_id": job_id,
            "total_pages": total_pages,
            "message": "PDF has more than 100 pages. Waiting for PC Owner approval."
        })

    return await execute_print_job(filename, file_path, print_type, copies, page_selection, layout_mode, is_preprocessed_image=False)

async def execute_print_job(filename, file_path, print_type, copies, page_selection="", layout_mode="normal", is_preprocessed_image=False):
    global TODAY_PRINT_COUNT
    if is_preprocessed_image:
        printable_images = [file_path]
    else:
        printable_images = process_file_to_printable_images(file_path, print_type, page_selection, layout_mode)

    rate = 10 if print_type == "color" else 3
    total_printed_pages = copies * len(printable_images)
    total_bill = rate * total_printed_pages

    TODAY_PRINT_COUNT += total_printed_pages

    print("\n-------------------------------------------------------")
    print("VICKY SMART KIOSK - FAST PRINT JOB IN QUEUE!")
    print(f"File Name    : {filename}")
    print(f"Mode         : {print_type.upper()}")
    print(f"Copies       : {copies}")
    print(f"Total Bill   : ₹{total_bill}")
    print("-------------------------------------------------------\n")

    try:
        if getattr(sys, 'frozen', False):
            base_path = os.path.dirname(sys.executable)
        else:
            base_path = os.path.dirname(os.path.abspath(__file__))

        sumatra_path = os.path.join(base_path, "SumatraPDF.exe")
        if not os.path.exists(sumatra_path):
            sumatra_path = os.path.join(base_path, "SumatraPDF.exe.exe")

        if os.path.exists(sumatra_path):
            print_queue.put((sumatra_path, printable_images, copies, SELECTED_PRINTER))
        else:
            print("[WARNING] SumatraPDF.exe folder mein nahi mila!")
    except Exception as e:
        print(f"[ERROR] Printing Error: {e}")
        raise Exception(f"Printer execution error: {e}")

    all_files_to_delete = [file_path] + printable_images
    auto_delete_files(all_files_to_delete, delay=120)

    return {
        "filename": filename,
        "copies": total_printed_pages,
        "print_type": print_type,
        "total_bill": total_bill
    }

@app.get("/check-approval/{job_id}")
async def check_approval(job_id: str):
    if job_id not in PENDING_APPROVALS:
        return {"status": "not_found"}
    
    job = PENDING_APPROVALS[job_id]
    if job["status"] == "approved":
        res = await execute_print_job(
            job["filename"], 
            job["file_path"], 
            job["print_type"], 
            job["copies"], 
            job["page_selection"], 
            job["layout_mode"]
        )
        del PENDING_APPROVALS[job_id]
        return {"status": "approved", "copies": res["copies"], "total_bill": res["total_bill"]}
    elif job["status"] == "rejected":
        del PENDING_APPROVALS[job_id]
        return {"status": "rejected"}
    
    return {"status": "pending"}

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()

    if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
        try:
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    import uvicorn
    import tkinter as tk
    from tkinter import messagebox, ttk
    from PIL import ImageTk

    TEMP_OTP_STORE = {}

    def open_login_window():
        login_root = tk.Tk()
        login_root.title("Vicky Smart Kiosk - Enterprise Partner Portal")
        login_root.geometry("420x550")
        login_root.configure(bg="#0f172a")
        login_root.resizable(False, False)

        tk.Label(login_root, text="VICKY SMART KIOSK", font=("Segoe UI", 16, "bold"), fg="#ffffff", bg="#0f172a").pack(pady=(15, 2))
        tk.Label(login_root, text="Self-Service Partner Signup & Login", font=("Segoe UI", 10), fg="#38bdf8", bg="#0f172a").pack(pady=(0, 15))

        card = tk.Frame(login_root, bg="#1e293b", padx=20, pady=15)
        card.pack(fill="both", expand=True, padx=20, pady=5)

        tk.Label(card, text="Shop Mobile Number:", font=("Segoe UI", 10, "bold"), fg="#cbd5e1", bg="#1e293b").pack(anchor="w")
        phone_entry = tk.Entry(card, font=("Segoe UI", 12), width=25, bg="#0f172a", fg="white", insertbackground="white")
        phone_entry.pack(pady=(3, 10), ipadx=5, ipady=3)

        tk.Label(card, text="Password / PIN:", font=("Segoe UI", 10, "bold"), fg="#cbd5e1", bg="#1e293b").pack(anchor="w")
        pass_entry = tk.Entry(card, font=("Segoe UI", 12), width=25, show="*", bg="#0f172a", fg="white", insertbackground="white")
        pass_entry.pack(pady=(3, 10), ipadx=5, ipady=3)

        otp_frame = tk.Frame(card, bg="#1e293b")
        otp_label = tk.Label(otp_frame, text="Enter 6-Digit Verification OTP:", font=("Segoe UI", 10, "bold"), fg="#38bdf8", bg="#1e293b")
        otp_entry = tk.Entry(otp_frame, font=("Segoe UI", 12), width=15, bg="#0f172a", fg="white", insertbackground="white")

        def request_otp():
            phone = phone_entry.get().strip()
            password = pass_entry.get().strip()
            if len(phone) != 10:
                messagebox.showerror("Error", "Kripya valid 10-digit mobile number bharein!")
                return
            if not password:
                messagebox.showerror("Error", "Kripya Password / PIN zaroor enter karein!")
                return
            
            db = load_db()
            if phone in db and db[phone]["password"] != password:
                messagebox.showerror("Error", "Yeh number pehle se registered hai aur password galat hai!")
                return

            otp = str(random.randint(100000, 999999))
            TEMP_OTP_STORE[phone] = otp
            messagebox.showinfo("OTP Sent", f"Aapka Verification OTP hai: {otp}")
            
            otp_frame.pack(fill="x", pady=5)
            otp_label.pack(anchor="w")
            otp_entry.pack(pady=3, ipadx=5, ipady=2)
            send_otp_btn.config(text="Verify & Launch System", bg="#10b981", command=verify_and_register_or_login)

        def verify_and_register_or_login():
            phone = phone_entry.get().strip()
            password = pass_entry.get().strip()
            entered_otp = otp_entry.get().strip()
            pc_sig = get_pc_signature()

            if TEMP_OTP_STORE.get(phone) != entered_otp:
                messagebox.showerror("Error", "Wrong OTP! Kripya sahi OTP dalein.")
                return

            db = load_db()

            for s_id, s_data in db.items():
                if s_data.get("pc_sig") == pc_sig and s_id != phone:
                    messagebox.showerror("Security Lock", "Yeh PC pehle se hi kisi aur Shop ID ke sath locked hai!")
                    return

            if phone not in db:
                db[phone] = {
                    "password": password, 
                    "pc_sig": pc_sig, 
                    "printer": "Default Printer",
                    "created_at": str(datetime.now())
                }
                save_db(db)
                messagebox.showinfo("Welcome!", f"Badhai ho! Aapka naya Shop Account ({phone}) successfully ban gaya hai!")
            else:
                if not db[phone].get("pc_sig"):
                    db[phone]["pc_sig"] = pc_sig
                    save_db(db)
                elif db[phone]["pc_sig"] != pc_sig:
                    messagebox.showerror("Security Error", "Yeh Shop ID kisi aur computer par registered hai!")
                    return

            global CURRENT_SHOP_ID, SELECTED_PRINTER
            CURRENT_SHOP_ID = phone
            SELECTED_PRINTER = db[phone].get("printer", "Default Printer")
            
            login_root.destroy()
            start_main_kiosk_dashboard(phone)

        def forgot_password():
            phone = phone_entry.get().strip()
            db = load_db()
            if phone not in db:
                messagebox.showerror("Error", "Yeh mobile number database mein registered nahi hai!")
                return
            
            otp = str(random.randint(100000, 999999))
            TEMP_OTP_STORE[phone] = otp
            messagebox.showinfo("Password Reset OTP", f"Password reset OTP for {phone} is: {otp}")
            
            reset_win = tk.Toplevel(login_root)
            reset_win.title("Reset Password")
            reset_win.geometry("300x250")
            reset_win.configure(bg="#0f172a")

            tk.Label(reset_win, text="Enter Reset OTP:", fg="white", bg="#0f172a").pack(pady=5)
            r_otp = tk.Entry(reset_win, show="*")
            r_otp.pack(pady=5)

            tk.Label(reset_win, text="New Password:", fg="white", bg="#0f172a").pack(pady=5)
            r_pass = tk.Entry(reset_win, show="*")
            r_pass.pack(pady=5)

            def save_new_pass():
                if TEMP_OTP_STORE.get(phone) == r_otp.get().strip():
                    db[phone]["password"] = r_pass.get().strip()
                    save_db(db)
                    messagebox.showinfo("Success", "Password successfully update ho gaya!")
                    reset_win.destroy()
                else:
                    messagebox.showerror("Error", "Invalid OTP!")

            tk.Button(reset_win, text="Update Password", bg="#2563eb", fg="white", command=save_new_pass).pack(pady=15)

        send_otp_btn = tk.Button(card, text="Get Verification OTP", font=("Segoe UI", 10, "bold"), fg="white", bg="#2563eb", command=request_otp, width=25, pady=6)
        send_otp_btn.pack(pady=10)

        forgot_btn = tk.Button(card, text="Forgot Password?", font=("Segoe UI", 9), fg="#38bdf8", bg="#1e293b", bd=0, command=forgot_password)
        forgot_btn.pack(pady=2)

        login_root.mainloop()

    def start_main_kiosk_dashboard(shop_owner):
        os.system("taskkill /f /im lt.exe >nul 2>&1")
        os.system("taskkill /f /im node.exe >nul 2>&1")

        def run_server():
            uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")

        server_thread = threading.Thread(target=run_server, daemon=True)
        server_thread.start()
        time.sleep(1)

        public_url = "http://127.0.0.1:8000"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            public_url = f"http://{local_ip}:8000"
        except Exception:
            pass

        try:
            lt_process = subprocess.Popen(
                ["lt", "--port", "8000"], 
                stdout=subprocess.PIPE, 
                stderr=subprocess.STDOUT, 
                text=True, 
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
            
            for _ in range(15):
                line = lt_process.stdout.readline()
                if "url" in line.lower():
                    parts = line.split()
                    for p in parts:
                        if p.startswith("http"):
                            public_url = p.strip()
                            break
                    break
                time.sleep(0.5)
        except Exception as e:
            print(f"[WARNING] Localtunnel connection warning (using local IP fallback): {e}")

        print("\n=======================================================")
        print(f"VICKY SMART KIOSK - Shop URL ({shop_owner}): {public_url}")
        print("======================================================-\n")
        
        qr_filename = f"qr_{shop_owner}.png"
        if qrcode:
            try:
                img = qrcode.make(public_url)
                img.save(qr_filename)
                print(f"Unique QR Code generated successfully for Shop ID: {shop_owner}!")
            except Exception as qe:
                print(f"QR Generation Error: {qe}")

        root = tk.Tk()
        root.title(f"Vicky Smart Kiosk - Control Panel [Shop ID: {shop_owner}]")
        root.geometry("480x780")
        root.configure(bg="#0f172a")
        root.resizable(False, False)

        tk.Label(root, text="VICKY SMART KIOSK", font=("Segoe UI", 16, "bold"), fg="#ffffff", bg="#0f172a").pack(pady=(15, 2))
        tk.Label(root, text=f"Partner Account: {shop_owner} (Active)", font=("Segoe UI", 9, "bold"), fg="#10b981", bg="#0f172a").pack(pady=2)

        status_frame = tk.Frame(root, bg="#1e293b", padx=10, pady=6, relief="flat")
        status_frame.pack(fill="x", padx=25, pady=8)
        tk.Label(status_frame, text="Status: ONLINE & LIVE", font=("Segoe UI", 10, "bold"), fg="#00ff88", bg="#1e293b").pack()

        count_frame = tk.Frame(root, bg="#1e293b", bd=1, relief="solid")
        count_frame.configure(highlightbackground="#334155", highlightthickness=1)
        count_frame.pack(fill="x", padx=25, pady=8)

        tk.Label(count_frame, text="TODAY'S TOTAL PRINTS / COPIES", font=("Segoe UI", 9, "bold"), fg="#94a3b8", bg="#1e293b").pack(pady=(8, 2))
        count_display_label = tk.Label(count_frame, text="0 Copies", font=("Segoe UI", 22, "bold"), fg="#f59e0b", bg="#1e293b")
        count_display_label.pack(pady=(0, 8))

        def update_gui_counter():
            global TODAY_PRINT_COUNT
            count_display_label.config(text=f"{TODAY_PRINT_COUNT} Copies")
            
            for j_id, j_data in list(PENDING_APPROVALS.items()):
                if j_data["status"] == "pending":
                    j_data["status"] = "processing"
                    approved = messagebox.askyesno(
                        "Large PDF Print Request (>100 Pages)", 
                        f"Badi PDF file upload ho chuki hai jisme 100 se zyada pages hain!\n\nFile: {j_data['filename']}\nTotal Pages: {j_data['total_pages']}\nCopies: {j_data['copies']}\n\nKya aap ise print karna chahte hain?"
                    )
                    if approved:
                        j_data["status"] = "approved"
                    else:
                        j_data["status"] = "rejected"

            root.after(1000, update_gui_counter)

        tk.Label(root, text="Select Active Thermal / Office Printer:", font=("Segoe UI", 10, "bold"), fg="#cbd5e1", bg="#0f172a").pack(anchor="w", padx=25, pady=(5, 2))
        
        printer_list = ["Default Printer"]
        try:
            printers = [printer[2] for printer in win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)]
            printer_list.extend(printers)
        except Exception as e:
            print(f"Printer fetch error: {e}")

        printer_dropdown = ttk.Combobox(root, values=printer_list, font=("Segoe UI", 10), state="readonly", width=38)
        
        global SELECTED_PRINTER
        db = load_db()
        if shop_owner in db and "printer" in db[shop_owner]:
            saved_printer = db[shop_owner]["printer"]
            if saved_printer in printer_list:
                SELECTED_PRINTER = saved_printer

        printer_dropdown.set(SELECTED_PRINTER)
        printer_dropdown.pack(padx=25, pady=5, ipady=3)

        def on_printer_select(event):
            global SELECTED_PRINTER
            SELECTED_PRINTER = printer_dropdown.get()
            
            db = load_db()
            if shop_owner in db:
                db[shop_owner]["printer"] = SELECTED_PRINTER
                save_db(db)
            print(f"Printer Saved & Changed To: {SELECTED_PRINTER}")

        printer_dropdown.bind("<<ComboboxSelected>>", on_printer_select)

        def test_printer_connection():
            try:
                p_name = SELECTED_PRINTER
                if p_name == "Default Printer":
                    hPrinter = win32print.OpenPrinter(win32print.GetDefaultPrinter())
                else:
                    hPrinter = win32print.OpenPrinter(p_name)
                win32print.ClosePrinter(hPrinter)
                messagebox.showinfo("Success", f"Printer '{p_name}' successfully connected aur ready hai!")
            except Exception as tp_err:
                messagebox.showerror("Printer Warning", f"Printer connection check failed: {tp_err}")

        tk.Button(root, text="Test Printer Connection", font=("Segoe UI", 9, "bold"), fg="white", bg="#3b82f6", command=test_printer_connection, width=32, pady=4).pack(pady=4)

        qr_container = tk.Frame(root, bg="#1e293b", padx=10, pady=10)
        qr_container.pack(padx=25, pady=6)
        
        if os.path.exists(qr_filename):
            try:
                qr_img = Image.open(qr_filename).resize((110, 110))
                qr_photo = ImageTk.PhotoImage(qr_img)
                img_label = tk.Label(qr_container, image=qr_photo, bg="#1e293b")
                img_label.image = qr_photo
                img_label.pack()
            except Exception as e:
                print(f"QR load error: {e}")

        tk.Label(root, text=public_url, font=("Segoe UI", 8), fg="#38bdf8", bg="#0f172a", wraplength=420).pack(pady=2)

        def stop_kiosk():
            os.system("taskkill /f /im lt.exe >nul 2>&1")
            os.system("taskkill /f /im node.exe >nul 2>&1")
            root.destroy()
            sys.exit()

        tk.Button(root, text="STOP KIOSK SERVER", font=("Segoe UI", 10, "bold"), fg="white", bg="#ef4444", activebackground="#dc2626", command=stop_kiosk, width=32, pady=6).pack(pady=8)

        root.after(1000, update_gui_counter)
        root.mainloop()

    open_login_window()