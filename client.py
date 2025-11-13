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
        self.pending_punch = None 
        self.root = root
        self.root.title("P2P 客戶端 (穩定續約 + 延遲打洞)")
        self.root.geometry("700x750")

        self.peer_id = self._load_or_generate_peer_id()
        self.signaling_ip = None
        self.signaling_port = None
        self.public_ip = "N/A"
        self.public_port = 0
        self.is_registered = False
        self.registration_in_progress = False

        self.active_peers = {}           # 伺服器回報的 Peer 列表 {id: {ip, port}}
        self.p2p_connections = {}        # 已建立的 P2P 通道 {id: (ip, port)}
        self.pending_message = {}        # 快取的待發訊息 {id: "message"}
        self.selected_target_id = None   # UI 選擇的目標
        
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
        tk.Label(frame_peers, text="活躍客戶端 ID (點擊選擇目標):", font=('Arial', 10, 'bold')).pack(anchor='w', pady=5)
        self.peer_list_box = tk.Listbox(frame_peers, height=6, font=('Courier', 10))
        self.peer_list_box.pack(fill='x', expand=True)
        self.peer_list_box.bind('<<ListboxSelect>>', self.on_peer_select)
        self.selected_peer_info = tk.Label(frame_peers, text="[尚未選擇目標]", fg='gray', font=('Arial', 10))
        self.selected_peer_info.pack(anchor='w', pady=(5, 0))

        frame_send = tk.Frame(self.root, padx=10, pady=5, bd=1, relief=tk.GROOVE)
        frame_send.pack(fill='x', pady=5)
        self.message_input = tk.Entry(frame_send, width=40)
        self.message_input.pack(side='left', padx=5, fill='x', expand=True)
        # *** 修改點：更新按鈕文字 ***
        tk.Button(frame_send, text="發送 (建立連線)", command=self.send_p2p_message_ui).pack(side='left', padx=5)

        frame_log = tk.Frame(self.root, padx=10, pady=5)
        frame_log.pack(fill='both', expand=True)
        tk.Label(frame_log, text="通信日誌:", font=('Arial', 10, 'bold')).pack(anchor='w', pady=(0, 5))
        # *** 修改點：增加防火牆提示 ***
        tk.Label(frame_log, text="提示: 若打洞失敗，請檢查本地防火牆是否允許 Python 或 UDP 50000 埠", fg='gray', font=('Arial', 8)).pack(anchor='w')
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

                # *** 修改點：收到打洞請求，建立 P2P 連線並檢查快取 ***
                elif t == "HOLE_PUNCH":
                    sender_id = msg.get('sender_id', '未知')
                    self.log(f"收到打洞 ← {addr[0]}:{addr[1]} (ID: {sender_id})", 'purple')
                    self._send_punch_ack(addr[0], addr[1])

                    # 儲存 P2P 連線
                    self.p2p_connections[sender_id] = (addr[0], addr[1])
                    self.log(f"打洞成功！可直連 {addr[0]}:{addr[1]}", 'blue')
                    
                    # 更新 UI
                    if self.selected_target_id == sender_id:
                        self.root.after(0, self.selected_peer_info.config, 
                        {'text': f"已直連 {addr[0]}:{addr[1]}", 'fg': 'green'})
                    
                    # 檢查並發送快取訊息
                    self._check_and_send_pending_message(sender_id, (addr[0], addr[1]))
    
                # *** 修改點：收到打洞回應，建立 P2P 連線並檢查快取 ***
                elif t == "PUNCH_ACK":
                    sender_id = msg.get('sender_id', '未知')
                    self.log(f"收到 PUNCH_ACK ← {addr[0]}:{addr[1]} (ID: {sender_id})", 'blue')
                    
                    # 儲存 P2P 連線
                    self.p2p_connections[sender_id] = (addr[0], addr[1])
                    
                    if self.pending_punch and self.pending_punch[0] == sender_id:
                        self.log(f"打洞成功！可直連 {addr[0]}:{addr[1]}", 'blue')
                        
                        # 更新 UI
                        if self.selected_target_id == sender_id:
                            self.root.after(0, self.selected_peer_info.config, 
                            {'text': f"已直連 {addr[0]}:{addr[1]}", 'fg': 'green'})
                        
                        self.pending_punch = None
                        
                        # 檢查並發送快取訊息
                        self._check_and_send_pending_message(sender_id, (addr[0], addr[1]))

                elif t == "MSG":
                    sender = msg.get('sender_id', '未知')
                    content = msg.get('content', '')
                    self.log(f"P2P 訊息 ← {sender} (來自 {addr[0]}:{addr[1]}): {content}", 'blue')

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
    # 7. P2P 流程（打洞 + 發送）
    # ==================================================================
    def request_peers(self):
        self.send_udp({"type": "get_peers", "requester_id": self.peer_id})

    def request_connect(self, target_id):
        self.send_udp({"type": "connect", "id": self.peer_id, "target_id": target_id})
        self.log(f"請求交換位址 → {target_id}", 'purple')
        self.root.after(0, self.selected_peer_info.config, 
                                {'text': f"正在請求 {target_id} 位址...", 'fg': 'orange'})

    def _initiate_hole_punching(self, target_id, ip, port):
        # *** 修改點：增加 Symmetric NAT 提示 ***
        self.log(f"打洞中 (Symmetric NAT 可能失敗) → {target_id} ({ip}:{port})", 'purple')
        self.pending_punch = (target_id, ip, port)
    
        msg = json.dumps({"type": "HOLE_PUNCH", "sender_id": self.peer_id}).encode('utf-8')
        for _ in range(5):
            self.udp_socket.sendto(msg, (ip, port))
            time.sleep(0.1)
    
        self.log(f"已發送 5 次打洞包，等待回應...", 'purple')
    
        def check_timeout():
            time.sleep(3)
            if self.pending_punch and self.pending_punch[0] == target_id:
                # *** 修改點：增加失敗原因提示 ***
                self.log(f"打洞失敗：{target_id} 無回應 (可能是 Symmetric NAT 或防火牆)", 'red')
                if self.selected_target_id == target_id:
                    self.root.after(0, self.selected_peer_info.config, 
                                    {'text': "打洞失敗 (NAT 或防火牆)", 'fg': 'red'})
                self.pending_punch = None
    
        threading.Thread(target=check_timeout, daemon=True).start()

    def _send_punch_ack(self, ip, port):
        msg = json.dumps({"type": "PUNCH_ACK", "sender_id": self.peer_id}).encode('utf-8')
        try:
            self.udp_socket.sendto(msg, (ip, port))
            self.log(f"已回應 PUNCH_ACK → {ip}:{port}", 'blue')
        except Exception as e:
            self.log(f"PUNCH_ACK 發送失敗: {e}", 'red')

    # *** 新增：內部 P2P 訊息發送函數 ***
    def _send_p2p_message_internal(self, target_id, content, address):
        """使用指定的 P2P 位址發送訊息"""
        try:
            msg = json.dumps({"type": "MSG", "sender_id": self.peer_id, "content": content}).encode('utf-8')
            self.udp_socket.sendto(msg, address)
            self.log(f"P2P 訊息 → {target_id} (at {address[0]}:{address[1]}): {content}", 'blue')
        except Exception as e:
            self.log(f"P2P 訊息發送失敗: {e}", 'red')

    # *** 新增：檢查並發送快取訊息 ***
    def _check_and_send_pending_message(self, sender_id, address):
        """檢查是否有待發訊息，如果有，就發送"""
        if sender_id in self.pending_message:
            content = self.pending_message.pop(sender_id)
            self.log(f"連線成功，發送快取訊息 → {sender_id}", 'green')
            self._send_p2p_message_internal(sender_id, content, address)
    
    # *** 修改點：UI 發送按鈕的邏輯 ***
    def send_p2p_message_ui(self):
        if not self.is_registered:
            messagebox.showwarning("未註冊", "請先註冊成功")
            return
            
        # 1. 檢查是否已從列表選擇目標
        if not self.selected_target_id:
            messagebox.showwarning("未選擇", "請先從列表中點擊一個目標")
            return
            
        target_id = self.selected_target_id
        content = self.message_input.get().strip()
        if not content:
            return
            
        # 2. 檢查 P2P 通道是否已建立
        if target_id in self.p2p_connections:
            # 情況 A：通道已建立，直接發送
            address = self.p2p_connections[target_id]
            self.log(f"使用已建立的 P2P 通道發送...", 'green')
            self._send_p2p_message_internal(target_id, content, address)
            self.message_input.delete(0, tk.END)
        else:
            # 情況 B：通道未建立，快取訊息並開始打洞
            self.pending_message[target_id] = content
            self.log(f"快取訊息: '{content}'", 'green')
            self.log(f"P2P 通道未建立，自動請求連線 → {target_id}", 'purple')
            
            # 觸發打洞流程
            self.request_connect(target_id)
            self.message_input.delete(0, tk.END)

    # *** 修改點：UI 列表選擇的邏輯 ***
    def on_peer_select(self, event):
        """當使用者點擊 Listbox 時觸發"""
        try:
            target_id = self.peer_list_box.get(self.peer_list_box.curselection()[0])
            self.selected_target_id = target_id
            
            # 更新 UI 標籤，顯示 P2P 狀態
            if target_id in self.p2p_connections:
                addr = self.p2p_connections[target_id]
                self.selected_peer_info.config(text=f"已直連: {addr[0]}:{addr[1]}", fg='green')
            elif target_id in self.active_peers:
                peer_info = self.active_peers.get(target_id)
                self.selected_peer_info.config(text=f"選定 (公網): {peer_info['ip']}:{peer_info['port']}", fg='blue')
            else:
                self.selected_peer_info.config(text="[選定 Peer 資訊: N/A]", fg='red')
                self.selected_target_id = None
        except:
            self.selected_peer_info.config(text="[尚未選擇目標]", fg='gray')
            self.selected_target_id = None

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