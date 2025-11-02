from flask import Blueprint, jsonify
import threading
import time
import ipaddress
import logging
import socket
import json
import atexit

# 設定日誌
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 創建 P2P 藍圖
p2p_server_bp = Blueprint('p2p_server', __name__)

# 全域字典：儲存已註冊的 Peer 資訊
registered_peers = {}
peers_lock = threading.Lock()

# 配置區塊
PEER_TIMEOUT = 60          # Peer 超時時間 (秒)
CLEANUP_INTERVAL = 10      # 清理間隔 (秒)
LISTEN_IP = '0.0.0.0'      # UDP 監聽 IP
LISTEN_PORT = 20042        # UDP 埠號
EXTERNAL_IP = '218.161.27.76'   # 公網 IP
EXTERNAL_PORT = 20042      # 公網埠號

# 單次執行標誌和鎖
P2P_SETUP_DONE = False
setup_lock = threading.Lock()


def start_cleanup_task():
    """啟動一個獨立的執行緒，定期清理超時的 Peer。"""
    def cleanup_loop():
        while True:
            time.sleep(CLEANUP_INTERVAL)
            with peers_lock:
                peers_to_remove = [
                    pid for pid, info in registered_peers.items()
                    if time.time() - info['last_seen'] > PEER_TIMEOUT
                ]
                if peers_to_remove:
                    for pid in peers_to_remove:
                        del registered_peers[pid]
                        logger.info(f"P2P 清理: Peer '{pid}' 超時被移除。")

    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    cleanup_thread.start()
    logger.info("Peer 清理執行緒已啟動。")


class SignalingUDPServerThread(threading.Thread):
    """UDP 信令伺服器執行緒"""
    def __init__(self):
        super().__init__()
        self.daemon = True
        self._stop_event = threading.Event()
        self.sock = None

    def run(self):
        logger.info(f"[Signaling UDP] 正在綁定 {LISTEN_IP}:{LISTEN_PORT}")

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((LISTEN_IP, LISTEN_PORT))
            self.sock.settimeout(1.0)

            logger.info(f"[Signaling UDP] 伺服器啟動於 {LISTEN_IP}:{LISTEN_PORT}")
            logger.info(f"[Signaling UDP] 報告的外部地址: {EXTERNAL_IP}:{EXTERNAL_PORT}")

            while not self._stop_event.is_set():
                try:
                    data, addr = self.sock.recvfrom(1024)
                    message = json.loads(data.decode('utf-8'))
                    msg_type = message.get('type')

                    # -------------------------------------------------
                    # 1. register
                    # -------------------------------------------------
                    if msg_type == "register":
                        peer_id = message.get('id')
                        if not peer_id:
                            continue

                        try:
                            ipaddress.ip_address(addr[0])
                        except ValueError:
                            logger.warning(f"[Signaling UDP] 無效 IP: {addr[0]} 從 {addr}")
                            continue

                        with peers_lock:
                            registered_peers[peer_id] = {
                                'ip': addr[0],
                                'port': addr[1],
                                'last_seen': time.time()
                            }

                        logger.info(f"[Signaling UDP] 註冊/續約: {peer_id} -> {addr[0]}:{addr[1]}")

                        # 關鍵：回應必須帶有 type = "register_success"
                        response = json.dumps({
                            "type": "register_success",
                            "public_ip": addr[0],
                            "public_port": addr[1]
                        }).encode('utf-8')
                        self.sock.sendto(response, addr)

                    # -------------------------------------------------
                    # 2. get_peers
                    # -------------------------------------------------
                    elif msg_type == "get_peers":
                        requester_id = message.get('requester_id')
                        current_time = time.time()
                        peers_list = []

                        with peers_lock:
                            # 順便清理超時的 peer
                            peers_to_remove = [
                                pid for pid, info in registered_peers.items()
                                if current_time - info['last_seen'] > PEER_TIMEOUT
                            ]
                            for pid in peers_to_remove:
                                del registered_peers[pid]
                                logger.info(f"[Signaling UDP] 獲取列表清理: Peer '{pid}' 超時被移除。")

                            for pid, info in registered_peers.items():
                                if pid != requester_id:
                                    peers_list.append({
                                        "id": pid,
                                        "ip": info['ip'],
                                        "port": info['port']
                                    })

                        # 回應必須帶有 type = "peers"
                        response = json.dumps({
                            "type": "peers",
                            "peers": peers_list
                        }).encode('utf-8')
                        self.sock.sendto(response, addr)
                        logger.info(f"[Signaling UDP] 返回 {len(peers_list)} 個 Peer 給 {requester_id}")

                    # -------------------------------------------------
                    # 3. connect
                    # -------------------------------------------------
                    elif msg_type == "connect":
                        peer_id = message.get('id')
                        target_id = message.get('target_id')
                        with peers_lock:
                            if peer_id in registered_peers and target_id in registered_peers:
                                my_info = registered_peers[peer_id]
                                target_info = registered_peers[target_id]

                                response_to_me = json.dumps({
                                    "type": "peer_addr",
                                    "id": target_id,
                                    "ip": target_info['ip'],
                                    "port": target_info['port']
                                }).encode('utf-8')
                                self.sock.sendto(response_to_me, (my_info['ip'], my_info['port']))

                                response_to_target = json.dumps({
                                    "type": "peer_addr",
                                    "id": peer_id,
                                    "ip": my_info['ip'],
                                    "port": my_info['port']
                                }).encode('utf-8')
                                self.sock.sendto(response_to_target, (target_info['ip'], target_info['port']))

                                logger.info(f"[Signaling UDP] 交換 addr: {peer_id} <-> {target_id}")

                except socket.timeout:
                    continue
                except json.JSONDecodeError:
                    logger.warning(f"[Signaling UDP] 無效 JSON 從 {addr}")
                except Exception as e:
                    logger.error(f"[Signaling UDP] 錯誤: {e}")

        except socket.error as e:
            logger.error(f"[Signaling UDP] Socket 錯誤: 無法綁定 {LISTEN_IP}:{LISTEN_PORT}。錯誤: {e}")
        finally:
            if self.sock:
                self.sock.close()
            logger.info("[Signaling UDP] 伺服器停止。")

    def stop(self):
        """安全停止 UDP 伺服器"""
        self._stop_event.set()
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
        logger.info("[Signaling UDP] 發出停止信號。")


# 單例實例
signaling_thread = SignalingUDPServerThread()


def start_signaling_server():
    """啟動 UDP 信令伺服器"""
    if not signaling_thread.is_alive():
        signaling_thread.start()


def stop_signaling_server():
    """停止 UDP 信令伺服器"""
    if signaling_thread.is_alive():
        signaling_thread.stop()


@p2p_server_bp.before_app_request
def setup_p2p_services():
    """在 Flask 應用處理第一個請求前啟動 P2P 服務，確保只執行一次。"""
    global P2P_SETUP_DONE
    if not P2P_SETUP_DONE:
        with setup_lock:
            if not P2P_SETUP_DONE:
                start_signaling_server()
                atexit.register(stop_signaling_server)
                start_cleanup_task()
                P2P_SETUP_DONE = True
                logger.info("P2P 服務初始化完成 (UDP 信令 & Peer Cleanup)。")


# HTTP 端點：返回伺服器資訊
@p2p_server_bp.route('/api/server-info', methods=['POST'])
def get_server_info():
    """返回伺服器 IP 和埠號資訊"""
    response = {
        "ip": EXTERNAL_IP,
        "port": EXTERNAL_PORT
    }
    return jsonify(response)