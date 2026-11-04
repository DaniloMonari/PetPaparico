#include <WiFi.h>
#include <WebServer.h>
#include <ESP32Servo.h>
#include <ArduinoJson.h>
#include <Preferences.h>

// ===== CONFIGURAÇÕES WI-FI =====
const char* ssid     = "DANILO MONARI";
const char* password = "db30051616";

// ===== CONFIGURAÇÕES DO DISPOSITIVO =====
const char* DEVICE_ID = "petfeeder_001";
const int SERVER_PORT = 80;

// ===== MAPEAMENTO DE PINOS FÍSICOS =====
const int PIN_SERVO_PWM   = 21;  // Sinal do servo MG996R na GPIO 21
const int PIN_HCSR04_TRIG = 27;  // Disparo do ultrassom (D27)
const int PIN_HCSR04_ECHO = 14;  // Retorno atenuado via divisor resistivo (D14)

// ===== PARÂMETROS DO SERVO =====
Servo servoMotor;
const int SERVO_POSICAO_FECHADO = 0;
const int SERVO_POSICAO_ABERTO  = 90;
const int TEMPO_DISPENSO_PADRAO = 2500; // ms

// Calibração do reservatório (vão livre)
const float DISTANCIA_CHEIO_CM = 5.0;
const float DISTANCIA_VAZIO_CM = 20.0; // Vão livre >= 20 cm = Alerta de compra

// ===== VARIÁVEIS GLOBAIS =====
WebServer server(SERVER_PORT);
Preferences preferences;

struct DeviceState {
  bool isOnline = true;
  int foodLevel = 0;           // % (0-100)
  float rawDistanceCm = 0.0;   // Vão livre em centímetros
  bool isCritico = false;      // true se >= 20 cm
  int batteryLevel = 100;      // Simulado
  bool isDispensing = false;
  unsigned long lastFeeding = 0;
} deviceState;

// Declarações prévias
void setupRoutes();
void conectarWiFi();
void atualizarNivelRacao();
bool dispensarRacao(int duracaoMs);
void sendJsonResponse(JsonDocument& doc, int code);

void setup() {
  Serial.begin(115200);
  delay(1000);
  
  Serial.println("\n=== Pet Feeder ESP32 Iniciando ===");
  
  pinMode(PIN_HCSR04_TRIG, OUTPUT);
  pinMode(PIN_HCSR04_ECHO, INPUT);
  
  // Inicialização do servomotor na GPIO 21
  ESP32PWM::allocateTimer(0);
  servoMotor.setPeriodHertz(50);
  servoMotor.attach(PIN_SERVO_PWM, 500, 2400);
  servoMotor.write(SERVO_POSICAO_FECHADO);
  
  // Carregar histórico de alimentação persistido
  preferences.begin("feeder", false);
  deviceState.lastFeeding = preferences.getULong("lastFeed", 0);
  
  conectarWiFi();
  setupRoutes();
  
  server.begin();
  Serial.println("[HTTP] Servidor iniciado na porta " + String(SERVER_PORT));
  
  atualizarNivelRacao();
}

void loop() {
  server.handleClient();
  
  // Atualiza a leitura do ultrassom a cada 5 segundos
  static unsigned long lastLevelCheck = 0;
  if (millis() - lastLevelCheck > 5000) {
    atualizarNivelRacao();
    lastLevelCheck = millis();
  }
}

// ===== CONEXÃO WI-FI =====
void conectarWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);
  
  Serial.print("[WI-FI] Conectando");
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30) {
    delay(500);
    Serial.print(".");
    attempts++;
  }
  
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n[WI-FI] Conectado!");
    Serial.print(">>> IP DO ESP32: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\n[WI-FI] Falha de conexao. Reiniciando placa...");
    ESP.restart();
  }
}

// ===== ROTAS HTTP =====
void setupRoutes() {
  // Rota raiz
  server.on("/", HTTP_GET, []() {
    StaticJsonDocument<256> doc;
    doc["device_id"] = DEVICE_ID;
    doc["name"] = "Pet Feeder ESP32";
    doc["status"] = "online";
    doc["ip"] = WiFi.localIP().toString();
    sendJsonResponse(doc, 200);
  });
  
  // Rota GET /api/status -> Consulta telemetria e nível
  server.on("/api/status", HTTP_GET, []() {
    StaticJsonDocument<512> doc;
    doc["device_id"] = DEVICE_ID;
    doc["online"] = deviceState.isOnline;
    doc["food_level"] = deviceState.foodLevel;
    doc["vao_livre_cm"] = deviceState.rawDistanceCm;
    doc["estoque_critico"] = deviceState.isCritico;
    doc["is_dispensing"] = deviceState.isDispensing;
    doc["wifi_rssi"] = WiFi.RSSI();
    doc["last_feeding"] = deviceState.lastFeeding;
    sendJsonResponse(doc, 200);
  });
  
  // Rota POST /api/feed -> Dispara a rotação da comporta
  server.on("/api/feed", HTTP_POST, []() {
    if (deviceState.isDispensing) {
      StaticJsonDocument<128> doc;
      doc["success"] = false;
      doc["error"] = "Dispensacao ja em andamento";
      sendJsonResponse(doc, 409);
      return;
    }
    
    int duration = TEMPO_DISPENSO_PADRAO;
    if (server.hasArg("plain")) {
      StaticJsonDocument<256> requestBody;
      deserializeJson(requestBody, server.arg("plain"));
      if (requestBody.containsKey("duration")) {
        duration = requestBody["duration"];
      }
    }
    
    bool success = dispensarRacao(duration);
    
    StaticJsonDocument<256> doc;
    doc["success"] = success;
    doc["timestamp"] = millis();
    doc["food_level_after"] = deviceState.foodLevel;
    doc["alerta_comprar_racao"] = deviceState.isCritico;
    
    sendJsonResponse(doc, success ? 200 : 400);
  });
}

// ===== ACIONAMENTO E LEITURAS =====
bool dispensarRacao(int duracaoMs) {
  atualizarNivelRacao();
  
  if (deviceState.isCritico) {
    Serial.println("[BLOQUEIO] Reservatorio vazio! Operacao cancelada.");
    return false;
  }
  
  deviceState.isDispensing = true;
  Serial.printf("[ATUADOR] Abrindo comporta no pino 21 por %d ms...\n", duracaoMs);
  servoMotor.write(SERVO_POSICAO_ABERTO);
  delay(duracaoMs);
  servoMotor.write(SERVO_POSICAO_FECHADO);
  Serial.println("[ATUADOR] Comporta fechada.");
  
  deviceState.isDispensing = false;
  deviceState.lastFeeding = millis();
  preferences.putULong("lastFeed", deviceState.lastFeeding);
  
  atualizarNivelRacao();
  return true;
}

void atualizarNivelRacao() {
  const int NUM_LEITURAS = 5;
  float leituras[NUM_LEITURAS];
  int validas = 0;

  for (int i = 0; i < NUM_LEITURAS; i++) {
    // Garante trigger limpo em LOW
    digitalWrite(PIN_HCSR04_TRIG, LOW);
    delayMicroseconds(4);
    
    // Disparo de 10 microssegundos
    digitalWrite(PIN_HCSR04_TRIG, HIGH);
    delayMicroseconds(10);
    digitalWrite(PIN_HCSR04_TRIG, LOW);

    // Timeout de 25000us (~4 metros)
    long duration = pulseIn(PIN_HCSR04_ECHO, HIGH, 25000);
    float d = (duration * 0.0343) / 2.0;

    // Filtra leituras absurdas (o HC-SR04 é cego abaixo de 2 cm)
    if (duration > 0 && d >= 2.0 && d <= 300.0) {
      leituras[validas] = d;
      validas++;
    }
    delay(30); // Essencial: aguarda o eco se dissipar antes do próximo disparo
  }

  if (validas == 0) {
    Serial.println("[ALERTA HC-SR04] Nenhuma leitura ultrassonica valida capturada.");
    return;
  }

  // Ordena as leituras para pegar a mediana (elimina picos e reflexões na parede do funil)
  for (int i = 0; i < validas - 1; i++) {
    for (int j = i + 1; j < validas; j++) {
      if (leituras[i] > leituras[j]) {
        float temp = leituras[i];
        leituras[i] = leituras[j];
        leituras[j] = temp;
      }
    }
  }

  // Mediana
  float distance = leituras[validas / 2];

  deviceState.rawDistanceCm = distance;
  deviceState.isCritico = (distance >= DISTANCIA_VAZIO_CM);

  // Mapeamento correto de porcentagem
  float clampedDist = constrain(distance, DISTANCIA_CHEIO_CM, DISTANCIA_VAZIO_CM);
  int percentage = map(clampedDist, DISTANCIA_VAZIO_CM, DISTANCIA_CHEIO_CM, 0, 100);
  deviceState.foodLevel = percentage;

  Serial.printf("[NIVEL REAL] Vao livre: %.2f cm | Calculado: %d%% | Critico: %s\n", 
                distance, percentage, deviceState.isCritico ? "SIM" : "NAO");
}

void sendJsonResponse(JsonDocument& doc, int code) {
  String response;
  serializeJson(doc, response);
  server.sendHeader("Content-Type", "application/json");
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(code, "application/json", response);
}
