import tkinter as tk
from tkinter import messagebox, scrolledtext
import threading
import time
import uuid
import socket
import json
import requests
import os
from datetime import datetime

class P2PClientApp:
    SIGNALING_SERVER_HOST = "clre20.ggff.net"
    API_ENDPOINT = "/api/server-info"
    LISTEN_PORT = 50000
    REGISTRATION_INTERVAL = 20  # 縮短到 20 秒，保持 NAT 映射
    BUFFER_SIZE = 1024
    MAX_HTTPS_RETRY = 3
    ID_FILE = "peer_id.txt"

    def __init__(self, root):
        self.registration_seq = 0
        self.root = root
        self.root.title("P2P 客戶端 (穩定續約 + 防卡死)")
        self.root.geometry("700x750")

        self.peer_id = self._load_or_generate_peer_id()
        self.signaling_ip = None
        self.signaling_port = None
        self.public_ip = "N/A"
        self.public_port = 0
        self.is_registered = False
        self.registration_in_progress = False

        self.active_peers = {}
        self.udp_socket = None
        self.listener_thread = None

        self.setup_ui()

        if not self.fetch_server_info_with_retry():
            self.set_status("狀態: 無法連線伺服器", 'red')
            self.connect_button.config(state='normal', text="點擊重試")
            return

        if not self.init_udp_socket():
            self.set_status("狀態: UDP 初始化失敗", 'red')
            return

        self.root.after(500, self.start_auto_registration)
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    # ==================================================================
    # 1. ID 持久化
    # ==================================================================
    def _load_or_generate_peer_id(self):
        if os.path.exists(self.ID_FILE):
            try:
                with open(self.ID_FILE, 'r', encoding='utf-8') as f:
                    pid = f.read().strip()
                if len(pid) == 32 and pid.isdigit():
                    self.log(f"讀取持久化 ID: {pid}", 'green')
                    return pid
            except:
                pass

        hex_id = str(uuid.uuid4()).replace('-', '')
        conversion_map = {'a': '1', 'b': '2', 'c': '3', 'd': '4', 'e': '5', 'f': '6'}
        numeric_id = ''.join(conversion_map.get(c, c) for c in hex_id)
        try:
            with open(self.ID_FILE, 'w', encoding='utf-8') as f:
                f.write(numeric_id)
            self.log(f"生成並儲存新 ID: {numeric_id}", 'green')
        except:
            self.log("儲存 ID 失敗", 'red')
        return numeric_id

    # ==================================================================
    # 2. HTTPS 獲取伺服器資訊
    # ==================================================================
    def fetch_server_info_with_retry(self):
        for attempt in range(self.MAX_HTTPS_RETRY):
            try:
                url = f"https://{self.SIGNALING_SERVER_HOST}{self.API_ENDPOINT}"
                self.log(f"正在獲取伺服器資訊 (第 {attempt + 1} 次)...", 'orange')
                response = requests.post(url, timeout=10)
                response.raise_for_status()
                data = response.json()
                self.signaling_ip = data.get("ip")
                self.signaling_port = data.get("port")
                if not self.signaling_ip or not self.signaling_port:
                    raise ValueError("格式錯誤")
                self.log(f"獲取成功: {self.signaling_ip}:{self.signaling_port}", 'green')
                return True
            except Exception as e:
                self.log(f"獲取失敗: {e}", 'red')
                time.sleep(2)
        return False

    # ==================================================================
    # 3. UI 設定
    # ==================================================================
    def setup_ui(self):
        frame_conn = tk.Frame(self.root, padx=10, pady=5)
        frame_conn.pack(fill='x')

        tk.Label(frame_conn, text="信令伺服器:", font=('Arial', 10, 'bold')).pack(side='left')
        self.server_input = tk.Entry(frame_conn, width=25)
        self.server_input.insert(0, self.SIGNALING_SERVER_HOST)
        self.server_input.config(state='readonly')
        self.server_input.pack(side='left', padx=5, fill='x', expand=True)

        self.connect_button = tk.Button(frame_conn, text="開始註冊", command=self.manual_retry)
        self.connect_button.pack(side='left', padx=5)

        self.status_label = tk.Label(frame_conn, text="狀態: 初始化中...", fg='gray', font=('Arial', 10, 'bold'), cursor="hand2")
        self.status_label.pack(side='left', padx=10)
        self.status_label.bind("<Button-1>", lambda e: self.manual_retry())

        frame_info = tk.Frame(self.root, padx=10, pady=5, bd=1, relief=tk.GROOVE)
        frame_info.pack(fill='x', pady=5)
        self.id_label = tk.Label(frame_info, text=f"ID: {self.peer_id}", font=('Courier', 10, 'bold'), fg='darkgreen')
        self.id_label.pack(side='left', padx=5)
        self.ip_label = tk.Label(frame_info, text="本地埠: N/A | 公網 IP: N/A", font=('Arial', 10))
        self.ip_label.pack(side='right', padx=5)

        frame_peers = tk.Frame(self.root, padx=10, pady=5, bd=1, relief=tk.GROOVE)
        frame_peers.pack(fill='x', pady=5)
        tk.Label(frame_peers, text="活躍客戶端 ID (點擊發起打洞):", font=('Arial', 10, 'bold')).pack(anchor='w', pady=5)
        self.peer_list_box = tk.Listbox(frame_peers, height=6, font=('Courier', 10))
        self.peer_list_box.pack(fill='x', expand=True)
        self.peer_list_box.bind('<<ListboxSelect>>', self.initiate_hole_punching_ui)
        self.selected_peer_info = tk.Label(frame_peers, text="[選定 Peer P2P 資訊: N/A]", fg='blue', font=('Arial', 10))
        self.selected_peer_info.pack(anchor='w', pady=(5, 0))

        frame_send = tk.Frame(self.root, padx=10, pady=5, bd=1, relief=tk.GROOVE)
        frame_send.pack(fill='x', pady=5)
        self.message_input = tk.Entry(frame_send, width=40)
        self.message_input.pack(side='left', padx=5, fill='x', expand=True)
        tk.Button(frame_send, text="發送 P2P 訊息", command=self.send_p2p_message_ui).pack(side='left', padx=5)

        frame_log = tk.Frame(self.root, padx=10, pady=5)
        frame_log.pack(fill='both', expand=True)
        tk.Label(frame_log, text="通信日誌:", font=('Arial', 10, 'bold')).pack(anchor='w', pady=(0, 5))
        self.log_area = scrolledtext.ScrolledText(frame_log, height=15, state='disabled', font=('Arial', 10))
        self.log_area.pack(fill='both', expand=True)

    def log(self, message, color='black'):
        timestamp = datetime.now().strftime("%H:%M:%S")
        full_msg = f"[{timestamp}] {message}"
        self.root.after(0, self._update_log_gui, full_msg, color)

    def _update_log_gui(self, message, color):
        self.log_area.config(state='normal')
        self.log_area.tag_config(color, foreground=color)
        self.log_area.insert(tk.END, message + '\n', color)
        self.log_area.config(state='disabled')
        self.log_area.see(tk.END)

    def set_status(self, text, color, button_text=None, button_state='disabled'):
        def update():
            self.status_label.config(text=text, fg=color)
            if button_text is not None:
                self.connect_button.config(text=button_text, state=button_state)
        self.root.after(0, update)

    # ==================================================================
    # 4. UDP 監聽（防卡死 + 超時）
    # ==================================================================
    def init_udp_socket(self):
        try:
            self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.udp_socket.bind(('0.0.0.0', self.LISTEN_PORT))
            self.udp_socket.settimeout(1.0)  # 關鍵：recvfrom 超時 1 秒
            self.listener_thread = threading.Thread(target=self._udp_listener, daemon=True)
            self.listener_thread.start()
            self.log(f"UDP 監聽啟動於埠 {self.LISTEN_PORT} (超時: 1s)", 'green')
            return True
        except Exception as e:
            self.log(f"UDP 初始化失敗: {e}", 'red')
            return False

    def _udp_listener(self):
        while True:
            try:
                data, addr = self.udp_socket.recvfrom(self.BUFFER_SIZE)
                msg = json.loads(data.decode('utf-8'))
                t = msg.get('type')

                if t == "register_success":
                    self.public_ip = msg['public_ip']
                    self.public_port = msg['public_port']
                    self.is_registered = True
                    self.registration_in_progress = False

                    self.set_status("狀態: 已註冊", 'green', "自動續約中...", 'disabled')
                    self.log(f"註冊成功！公網位址: {self.public_ip}:{self.public_port}", 'blue')
                    self.root.after(0, self.ip_label.config, {'text': f"本地埠: {self.LISTEN_PORT} | 公網 IP: {self.public_ip}:{self.public_port}"})
                    self.request_peers()

                elif t == "peers":
                    peers = msg.get('peers', [])
                    self.active_peers = {p['id']: {'ip': p['ip'], 'port': p['port']} for p in peers if p['id'] != self.peer_id}
                    self.log(f"收到 {len(self.active_peers)} 個活躍 Peer", 'green')
                    self.root.after(0, self._update_peer_list_box)

                elif t == "peer_addr":
                    tid, ip, port = msg['id'], msg['ip'], msg['port']
                    self.log(f"開始打洞 → {tid} ({ip}:{port})", 'purple')
                    threading.Thread(target=self._initiate_hole_punching, args=(tid, ip, port), daemon=True).start()

                elif t == "HOLE_PUNCH":
                    self.log(f"收到打洞 ← {addr[0]}:{addr[1]}", 'purple')
                    self._send_punch_ack(addr[0], addr[1])

                elif t == "PUNCH_ACK":
                    self.log(f"打洞成功！可直連 {addr[0]}:{addr[1]}", 'blue')

                elif t == "MSG":
                    sender = msg.get('sender_id', '未知')
                    content = msg.get('content', '')
                    self.log(f"P2P 訊息 ← {sender}: {content}", 'blue')

            except socket.timeout:
                continue  # 超時就跳過，繼續監聽
            except json.JSONDecodeError:
                continue
            except Exception as e:
                self.log(f"UDP 接收錯誤: {e}", 'red')
                continue

    # ==================================================================
    # 5. 註冊流程（防靜默失敗）
    # ==================================================================
    def register(self):
        current_seq = self.registration_seq
        self.registration_seq += 1

        self.set_status("狀態: 註冊中...", 'orange', "註冊中...", 'disabled')

        if self.send_udp({"type": "register", "id": self.peer_id}):
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.log(f"發送註冊請求 (seq={current_seq}) → ID: {self.peer_id} | 時間: {timestamp}", 'orange')
        else:
            self.set_status("狀態: 發送失敗", 'red', "點擊重試", 'normal')
            return

        def timeout_check():
            time.sleep(5)
            # 只有這個 seq 還在進行中，才處理
            if self.registration_seq - 1 == current_seq and not self.is_registered:
                self.set_status("狀態: 註冊超時 (自動重試中...)", 'orange', "自動重試中...", 'disabled')
                self.log(f"註冊超時 (seq={current_seq})，自動重試...", 'orange')
                self.root.after(100, self.register)

        threading.Thread(target=timeout_check, daemon=True).start()
    
    def send_udp(self, msg):
        if not self.signaling_ip or not self.udp_socket:
            self.log("UDP 未初始化，無法發送", 'red')
            return False
        try:
            data = json.dumps(msg).encode('utf-8')
            bytes_sent = self.udp_socket.sendto(data, (self.signaling_ip, self.signaling_port))
            if bytes_sent != len(data):
                self.log(f"UDP 發送不完整: {bytes_sent}/{len(data)} bytes", 'red')
                return False
            return True
        except Exception as e:
            self.log(f"UDP 發送失敗: {e}", 'red')
            return False

    # ==================================================================
    # 6. 自動續約 + 手動重試
    # ==================================================================
    def start_auto_registration(self):
        def loop():
            while True:
                if not self.registration_in_progress:
                    self.register()
                time.sleep(self.REGISTRATION_INTERVAL)
        threading.Thread(target=loop, daemon=True).start()

    def manual_retry(self):
        if self.is_registered:
            self.log("已在連線中，無需重試", 'orange')
            return
        self.log("手動觸發註冊重試", 'orange')
        self.is_registered = False
        self.registration_in_progress = False
        self.register()

    # ==================================================================
    # 7. 其他功能
    # ==================================================================
    def request_peers(self):
        self.send_udp({"type": "get_peers", "requester_id": self.peer_id})

    def request_connect(self, target_id):
        self.send_udp({"type": "connect", "id": self.peer_id, "target_id": target_id})
        self.log(f"請求連線 → {target_id}", 'purple')

    def _initiate_hole_punching(self, target_id, ip, port):
        self.log(f"打洞中 → {target_id} ({ip}:{port})", 'purple')
        msg = json.dumps({"type": "HOLE_PUNCH", "sender_id": self.peer_id}).encode('utf-8')
        for _ in range(5):
            self.udp_socket.sendto(msg, (ip, port))
            time.sleep(0.1)
        self.log(f"已發送 5 次打洞包", 'purple')

    def _send_punch_ack(self, ip, port):
        self.udp_socket.sendto(json.dumps({"type": "PUNCH_ACK", "sender_id": self.peer_id}).encode('utf-8'), (ip, port))

    def send_p2p_message_ui(self):
        if not self.is_registered:
            messagebox.showwarning("未註冊", "請先註冊成功")
            return
        try:
            target_id = self.peer_list_box.get(self.peer_list_box.curselection()[0])
        except:
            messagebox.showwarning("未選擇", "請選擇目標")
            return
        content = self.message_input.get().strip()
        if not content:
            return
        peer = self.active_peers.get(target_id)
        if not peer:
            return
        msg = json.dumps({"type": "MSG", "sender_id": self.peer_id, "content": content}).encode('utf-8')
        self.udp_socket.sendto(msg, (peer['ip'], peer['port']))
        self.log(f"P2P 訊息 → {target_id}: {content}", 'blue')
        self.message_input.delete(0, tk.END)

    def initiate_hole_punching_ui(self, event):
        try:
            target_id = self.peer_list_box.get(self.peer_list_box.curselection()[0])
            peer = self.active_peers.get(target_id)
            if peer:
                self.selected_peer_info.config(text=f"連線資訊: {peer['ip']}:{peer['port']}", fg='blue')
                self.request_connect(target_id)
        except:
            self.selected_peer_info.config(text="[選定 Peer P2P 資訊: N/A]", fg='red')

    def _update_peer_list_box(self):
        self.peer_list_box.delete(0, tk.END)
        for pid in self.active_peers:
            self.peer_list_box.insert(tk.END, pid)

    def on_closing(self):
        self.is_registered = False
        if self.udp_socket:
            self.udp_socket.close()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = P2PClientApp(root)
    root.mainloop()