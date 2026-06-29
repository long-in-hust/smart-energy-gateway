# ⚡ Smart Energy Management Gateway

**Môn học:** Lập trình và ảo hóa cho IoT — IT6130  
**Đề tài 5:** Virtual Smart Energy Management Gateway  
**Công cụ:** Docker, Docker Compose, Mosquitto MQTT, Python, InfluxDB, Grafana, FastAPI

---

## 📋 Mô tả hệ thống

Hệ thống giả lập việc quản lý điện năng cho một ngôi nhà thông minh (smart home). Toàn bộ thiết bị đều là phần mềm Python chạy trong Docker container — không cần phần cứng thật.

**Ý tưởng chính:** Nhà có 3 nhóm thiết bị tiêu thụ điện (điều hòa, đèn, ổ cắm) và 1 tấm pin mặt trời. Một gateway trung tâm theo dõi tổng công suất tiêu thụ, nếu vượt ngưỡng 3500W thì tự động tắt bớt thiết bị ít quan trọng để tránh quá tải. Khi mặt trời phát điện nhiều, gateway bật lại các thiết bị đã tắt trước đó.

### Các thành phần

| Thành phần | Giải thích |
|------------|------------|
| `meter-hvac` | Giả lập đồng hồ đo điện của điều hòa — publish số liệu lên MQTT mỗi 5 giây |
| `meter-lighting` | Giả lập đồng hồ đo điện của hệ thống đèn |
| `meter-plug` | Giả lập đồng hồ đo điện của tất cả thiết bị cắm ổ cắm (TV, tủ lạnh, máy tính...) |
| `solar-simulator` | Giả lập tấm pin mặt trời — công suất tăng dần từ 6h sáng, đạt đỉnh trưa, về 0 lúc 18h |
| `load-hvac/lighting/plug` | Giả lập công tắc điện — nhận lệnh bật/tắt từ gateway, phản hồi trạng thái |
| `energy-gateway` | Bộ não hệ thống — nhận dữ liệu từ tất cả meter, phân tích, ra lệnh điều khiển |
| `energy-api` | REST API để xem trạng thái và điều khiển thủ công qua HTTP |
| `mosquitto` | MQTT broker — trung gian chuyển message giữa tất cả service |
| `influxdb` | Database lưu toàn bộ dữ liệu theo thời gian |
| `grafana` | Dashboard hiển thị đồ thị realtime |

---

## 🏗️ Luồng dữ liệu

```
[meter-hvac]     ──┐
[meter-lighting]   ├──► MQTT ──► [energy-gateway] ──► [InfluxDB]
[meter-plug]     ──┘    │              │                   │
[solar-simulator] ───────┘         Rule Engine         [Grafana]
                                       │
                              [energy/{load}/load/command]
                                       │
                         [load-hvac / load-lighting / load-plug]
                                       │
                              publish status ──────────► [energy-gateway]
                                                               │
                                                         [energy-api]
                                                         REST :8000
```

**Giao thức sử dụng:**
- Meter → Gateway, Gateway → Actuator: **MQTT**
- Gateway → InfluxDB, Grafana → InfluxDB: **HTTP**
- Client → energy-api: **HTTP REST**

---

## 🚀 Chạy hệ thống

### Bước 1 — Chuẩn bị

```bash
git clone git@github.com:HangBich/smart-energy-gateway.git
cd smart-energy-gateway
cp .env.example .env
```

File `.env` chứa toàn bộ cấu hình. Mặc định chạy được ngay, chỉ cần đổi nếu muốn thay mật khẩu.

### Bước 2 — Khởi động

```bash
docker compose up -d --build
```

Lần đầu build mất 2-3 phút. Sau đó chờ thêm ~30 giây để tất cả service healthy.

### Bước 3 — Kiểm tra

```bash
docker compose ps
```

Kết quả mong đợi — tất cả phải `running`, riêng `mosquitto-init` là `exited 0` (bình thường):

```
energy-api        Up (unhealthy → healthy sau ~30s)
energy-gateway    Up (healthy)
grafana           Up (healthy)
influxdb          Up (healthy)
load-hvac         Up
load-lighting     Up
load-plug         Up
meter-hvac        Up (healthy)
meter-lighting    Up (healthy)
meter-plug        Up (healthy)
mosquitto         Up (healthy)
mosquitto-init    Exited (0)
solar-simulator   Up
```

---

## 🌐 Truy cập

| Service | URL | Tài khoản |
|---------|-----|-----------|
| **Grafana Dashboard** | http://localhost:3000 | admin / grafana_pass_2026 |
| **InfluxDB** | http://localhost:8086 | admin / admin_pass_2026 |
| **REST API** | http://localhost:8000 | — |
| **API Docs (Swagger)** | http://localhost:8000/docs | — |

### Lần đầu mở Grafana

Grafana provision dashboard tự động. Nếu thấy **"No data"**, làm theo:

1. Vào **Connections → Data sources → influxdb-1**
2. Tắt toggle **"Basic auth"** (phải để OFF)
3. Kéo xuống **InfluxDB Details**, điền:
   - Organization: `smart-energy`
   - Token: `energy-super-secret-token-2026`
   - Default Bucket: `energy_data`
4. Bấm **Save & test** → phải thấy `datasource is working`
5. Vào dashboard → chọn `influxdb-1` ở dropdown **DS_INFLUXDB** góc trên trái

---

## 📡 MQTT Topics

| Topic | Chiều | Mô tả |
|-------|-------|--------|
| `energy/{load}/meter/telemetry` | Meter → Gateway | Dữ liệu đo điện (power, current, voltage...) |
| `energy/solar/telemetry` | Solar → Gateway | Công suất pin mặt trời |
| `energy/{load}/load/command` | Gateway → Actuator | Lệnh bật/tắt |
| `energy/{load}/load/status` | Actuator → Gateway | Phản hồi sau khi nhận lệnh |
| `energy/gateway/summary` | Gateway → All | Tổng công suất mỗi 5 giây |
| `energy/gateway/event` | Gateway → All | Sự kiện bất thường (overload, offline...) |
| `energy/gateway/config` | API → Gateway | Cập nhật ngưỡng overload |

**Subscribe để xem message realtime:**

```bash
# Xem tất cả
docker compose exec mosquitto mosquitto_sub \
  -u energy_user -P energy_pass123 -t "energy/#" -v

# Chỉ xem event bất thường
docker compose exec mosquitto mosquitto_sub \
  -u energy_user -P energy_pass123 -t "energy/gateway/event" -v

# Chỉ xem tổng công suất
docker compose exec mosquitto mosquitto_sub \
  -u energy_user -P energy_pass123 -t "energy/gateway/summary" -v
```

---

## 🔌 REST API

### Xem trạng thái

```bash
# Kiểm tra API còn chạy không
curl http://localhost:8000/health

# Danh sách 3 loads với switch và power hiện tại
curl http://localhost:8000/loads

# Chi tiết 1 load
curl http://localhost:8000/loads/hvac/state
curl http://localhost:8000/loads/lighting/state
curl http://localhost:8000/loads/plug/state

# Tổng quan điện năng (total/solar/grid/overload)
curl http://localhost:8000/energy/summary

# Xem events gần nhất
curl http://localhost:8000/events
```

### Điều khiển thủ công

```bash
# Tắt một load
curl -X POST http://localhost:8000/loads/plug/command \
  -H "Content-Type: application/json" \
  -d '{"action": "off", "reason": "manual_control"}'

# Bật lại
curl -X POST http://localhost:8000/loads/plug/command \
  -H "Content-Type: application/json" \
  -d '{"action": "on", "reason": "manual_restore"}'

# Cập nhật ngưỡng overload (bonus endpoint)
curl -X PUT http://localhost:8000/config/threshold \
  -H "Content-Type: application/json" \
  -d '{"threshold_watt": 4000}'
```

### Kích hoạt Load Switch Status trên Grafana

Panel Load Switch Status cần actuator đã từng publish status. Chạy lệnh sau sau khi stack khởi động:

```bash
for load in hvac lighting plug; do
  curl -s -X POST http://localhost:8000/loads/$load/command \
    -H "Content-Type: application/json" \
    -d '{"action": "on", "reason": "init_status"}' > /dev/null
done
echo "Done — chờ 10s rồi refresh Grafana"
```

---

## 🔧 Rule Engine — 5 luật tự động

Gateway chạy rule engine mỗi 5 giây (và ngay khi nhận telemetry mới):

| # | Điều kiện | Hành động |
|---|-----------|-----------|
| 1 | Mọi lúc | Tính `total_power = Σ power_watt` của các load đang bật |
| 2 | `total_power > 3500W` | Sinh event `overload_detected` |
| 3 | Đang overload | Tắt load priority thấp nhất trước (low → medium, không tắt high) |
| 4 | `solar_power > 500W` + có load bị tắt do overload | Bật lại load nếu projected power < 85% ngưỡng |
| 5 | Meter không gửi data quá 30 giây | Sinh event `meter_offline` |

---

## 💉 Kiểm thử overload

Inject dữ liệu công suất cao để trigger rule engine:

```bash
# Cần paho-mqtt
pip install paho-mqtt

# Inject overload (publish 3 round × 5s = 15s)
python tests/inject_anomaly.py --scenario overload

# Theo dõi gateway phản ứng
docker compose logs -f energy-gateway | grep -E "overload|Command sent|Shedding|Event"
```

Script publish `hvac=2200W + lighting=900W + plug=1500W = 4600W` trong 15 giây — đủ để rule engine evaluate ít nhất 2 lần và trigger overload shedding.

---

## 🧪 Unit Tests

```bash
pip install pytest
python -m pytest tests/ -v --tb=short
```

29 test case — không cần MQTT hay InfluxDB, chạy offline hoàn toàn.

---

## 📊 Kiểm tra dữ liệu InfluxDB

Mở http://localhost:8086 → Data Explorer → Script Editor:

```flux
# Xem tất cả data 30 phút gần nhất
from(bucket: "energy_data")
  |> range(start: -30m)
  |> limit(n: 10)

# Xem công suất từng load
from(bucket: "energy_data")
  |> range(start: -30m)
  |> filter(fn: (r) => r._measurement == "meter_telemetry")
  |> filter(fn: (r) => r._field == "power_watt")

# Xem events overload
from(bucket: "energy_data")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "gateway_events")
  |> filter(fn: (r) => r.event_type == "overload_detected")
```

**5 measurement trong bucket `energy_data`:**
- `meter_telemetry` — power, current, voltage, energy_wh theo từng load
- `solar_telemetry` — công suất solar và irradiance
- `energy_summary` — total/solar/grid power, overload flag
- `gateway_events` — events với severity và message
- `load_status` — trạng thái bật/tắt từng load

---

## 🔍 Xem log

```bash
# Gateway — xem rule engine chạy, events, commands
docker compose logs -f energy-gateway

# API
docker compose logs -f energy-api

# Meter HVAC
docker compose logs -f meter-hvac

# Tất cả
docker compose logs -f
```

---

## ❗ Lỗi thường gặp

| Triệu chứng | Nguyên nhân | Cách sửa |
|-------------|-------------|----------|
| Grafana hiển thị "No data" | Basic auth đang bật trong datasource | Tắt Basic auth trong Connections → Data sources → Save & test |
| `energy-api` unhealthy | File state chưa được tạo | Chờ 30s sau khi gateway healthy |
| Mosquitto restart liên tục | `mosquitto-init` chưa tạo xong file passwd | Chờ 30s hoặc `docker compose restart mosquitto` |
| Gateway không kết nối được MQTT | Mosquitto chưa healthy | Gateway tự retry, chờ thêm 30s |
| Inject overload không trigger event | Meter thật đang ghi đè state | Script đã fix — publish 3 lần × 5s để đảm bảo rule engine evaluate |
| Load Switch Status "No data" | Actuator chưa publish status lần nào | Gửi manual command đến 3 loads (xem phần REST API) |

---

## 🛑 Dừng hệ thống

```bash
# Dừng nhưng giữ data (khởi động lại vẫn còn data cũ)
docker compose down

# Dừng và xóa sạch toàn bộ data
docker compose down -v
```

---

## 📁 Cấu trúc thư mục

```
smart-energy-gateway/
├── docker-compose.yml          ← định nghĩa 12 service
├── .env                        ← cấu hình (copy từ .env.example)
├── .env.example                ← template cấu hình
├── README.md
├── mosquitto/
│   └── config/
│       └── mosquitto.conf      ← cấu hình MQTT auth
├── smart_meter/
│   ├── meter.py                ← giả lập đồng hồ điện
│   ├── requirements.txt
│   └── Dockerfile
├── load_actuator/
│   ├── actuator.py             ← giả lập công tắc điện
│   ├── requirements.txt
│   └── Dockerfile
├── solar_simulator/
│   ├── solar.py                ← giả lập pin mặt trời
│   ├── requirements.txt
│   └── Dockerfile
├── energy_gateway/
│   ├── gateway.py              ← gateway chính (subscribe, validate, dispatch)
│   ├── rule_engine.py          ← 5 luật xử lý tự động
│   ├── state_store.py          ← lưu trạng thái thread-safe + persist JSON
│   ├── requirements.txt
│   └── Dockerfile
├── energy_api/
│   ├── api.py                  ← REST API FastAPI
│   ├── requirements.txt
│   └── Dockerfile
├── grafana/
│   └── provisioning/
│       ├── datasources/
│       │   └── influxdb.yml    ← auto-config datasource
│       └── dashboards/
│           ├── dashboard.yml
│           └── energy_dashboard.json   ← 8 panels
└── tests/
    ├── test_rule_engine.py     ← 25 unit tests
    ├── test_state_store.py     ← 12 unit tests
    ├── inject_anomaly.py       ← script test overload/solar/zero
    └── requirements-test.txt
```

---

## 👥 Phân công

| Thành viên | Nhiệm vụ |
|------------|----------|
| Vũ Khương Duy | `smart_meter/`, `load_actuator/`, `solar_simulator/` — thiết kế MQTT topics và message format |
| Trần Hoàng Long | `energy_gateway/` — gateway, rule engine, InfluxDB integration, state store |
| Nguyễn Thị Bich Hằng | `energy_api/`, `docker-compose.yml`, Grafana dashboard, README, unit tests |

---