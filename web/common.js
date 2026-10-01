// 서버 주소 설정. 배포 환경에 맞게 여기만 바꾼다.
// 라즈베리파이 시뮬레이터(io_server.py, Socket.IO; 5000 은 macOS AirPlay 가 점유)
// 페이지를 연 호스트 이름을 그대로 쓴다: 원격 서버(VM)에 접속해도 브라우저 PC 의 localhost 로 가지 않게.
const SERVER_URL = "http://" + (location.hostname || "localhost") + ":5001";
// FMS 서버(fms_server, FastAPI). 지도·로봇 상태는 전부 여기서 받는다.
// fms_server 가 /ui 로 서빙하는 페이지면 같은 오리진을 쓰고(CORS 불필요), 아니면 아래 기본값.
const FMS_ORIGIN = location.pathname.startsWith("/ui/") ? location.origin : "http://localhost:8000";
const FMS_API = FMS_ORIGIN + "/api/v1";
const FMS_WS_URL = FMS_ORIGIN.replace(/^http/, "ws") + "/api/v1/robots/stream";
