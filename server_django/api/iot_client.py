"""
Cliente IoT seguro para comunicação interna com ESP32
"""
import time
import requests
from typing import Optional, Dict, Any
from django.conf import settings


class ESP32Client:
    """Cliente HTTP interno do Django - não expõe IP nem simula dados"""
    
    def __init__(self, base_url: str = None):
        # Lê a variável de ambiente segura (ESP32_HOST) ou adota o IP da bancada
        self.base_url = base_url or getattr(settings, 'ESP32_BASE_URL', 'http://192.168.1.90')
        self.timeout = 4  # segundos
        
        # Desativa a simulação interna definitivamente
        self.simulator_enabled = False

    def _request(self, method: str, endpoint: str, data: Dict = None) -> Optional[Dict]:
        """Faz a chamada HTTP direta para a placa pela rede local"""
        url = f"{self.base_url.rstrip('/')}{endpoint}"
        try:
            if method == "GET":
                response = requests.get(url, timeout=self.timeout)
            elif method == "POST":
                response = requests.post(url, json=data, timeout=self.timeout)
            else:
                return None
            
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e), "online": False}

    def get_status(self, channel: int = None) -> Optional[Dict]:
        """Consulta a telemetria do ESP32 via rota /api/status"""
        data = self._request("GET", "/api/status")
        if data and not data.get("error"):
            return {
                "online": True,
                "food_level": data.get("food_level", 0),
                "vao_livre_cm": data.get("vao_livre_cm", 0.0),
                "estoque_critico": data.get("estoque_critico", False),
                "is_dispensing": data.get("is_dispensing", False),
                "device_id": data.get("device_id", "petfeeder_001"),
                "last_feeding": data.get("last_feeding", None),
                "simulated": False
            }
        return {"online": False, "simulated": False}

    def dispense_food(self, quantity: int = 50, duration_ms: int = 2500, 
                      feeding_id: str = None, channel: int = None) -> Dict[str, Any]:
        """Envia comando POST /api/feed para o servomotor na GPIO 21"""
        payload = {
            "duration": duration_ms,
            "quantity": quantity,
            "feeding_id": feeding_id or f"feed_{int(time.time())}"
        }
        res = self._request("POST", "/api/feed", payload)
        if res and res.get("success"):
            return res
        return res or {"success": False, "error": "Falha na comunicação com o ESP32"}

    def is_online(self, channel: int = None) -> bool:
        """Verifica se o microcontrolador está acessível"""
        st = self.get_status(channel)
        return bool(st and st.get("online", False))

    def calibrate_servo(self, position: int = 0, channel: int = None) -> Optional[Dict]:
        """Comando de calibração se necessário"""
        position = max(0, min(90, position))
        return self._request("POST", "/api/calibrate", {"position": position})


# Instância única utilizada por todo o Django
esp32_client = ESP32Client()