# Flow Music Kit

Hệ thống tự động tạo bài hát bằng **Google Flow Music** (`flowmusic.app`,
model Lyria 3.5) và dựng **music video** cho chúng. Đây là dự án anh em của
[Flow Kit](../flowkit): cùng kiến trúc (Chrome extension làm cầu nối + agent
Python + SQLite + skills cho Claude Code), và dùng Flow Kit để tạo phần hình cho
music video.

```
Claude Code (/fm-*) ──HTTP──▶ Agent FastAPI :8300 ──WS :9300──▶ Chrome extension
                                  │  SQLite, worker,                 │ chạy request trong
                                  │  ffmpeg, recipes                 ▼ tab flowmusic.app
                                  └──HTTP──▶ Flow Kit :8100 (hình ảnh/video cho MV)
```

## Điểm khác biệt quan trọng: recipe

Flow Music không có API công khai, và payload nội bộ của nó chưa được công bố
ở đâu. Thay vì đoán (sai một vị trí trong payload thường vẫn được chấp nhận rồi
bị bỏ qua âm thầm), mỗi thao tác được **ghi lại một lần từ UI thật**:

1. Bật capture mode → bạn bấm "Create" trên Flow Music với giá trị mẫu dễ nhận
   (vd. prompt `zzsample neon rain`).
2. Extension ghi body request + response (không ghi header/cookie; token `at`
   bị xoá).
3. Agent thay giá trị mẫu bằng biến (`{{prompt}}`, `{{lyrics}}`, `{{bpm|jsonval}}`),
   thay reCAPTCHA token bằng `__CAPTCHA__`, và lưu thành `recipes/generate_song.json`.
4. Từ đó agent replay recipe với nội dung mới, trong chính tab đã đăng nhập.

Claude làm việc này cho bạn qua `/fm-capture`. Khi Flow Music đổi UI, chỉ cần
ghi lại recipe bị hỏng.

## Cài đặt

Yêu cầu: macOS/Linux, Python 3.10+, ffmpeg, Chrome, tài khoản Google có quyền
dùng Flow Music.

```bash
./setup.sh          # tạo venv, cài thư viện, kiểm tra ffmpeg/Chrome
./run.sh            # agent chạy ở http://127.0.0.1:8300
```

Chrome → `chrome://extensions` → bật Developer mode → **Load unpacked** → chọn
thư mục `extension/`. Mở https://flowmusic.app, đăng nhập, và để tab mở
(nên pin tab để Chrome không discard).

Kiểm tra: `curl -s http://127.0.0.1:8300/api/status`

## Lần đầu: ghi recipe

Trong Claude Code tại thư mục này:

```
/fm-capture generate_song      # tạo bài hát + các lệnh poll đi kèm
/fm-capture poll_song
```

Tuỳ chọn, khi cần: `generate_lyrics`, `extend_song`, `cover_song`,
`upload_audio`, `generate_music_video`.

## Dùng hằng ngày

```
/fm-create-project             # album/kênh + phong cách chung + danh sách bài
/fm-write-lyrics <project_id>  # Claude viết lời [Verse]/[Chorus]
/fm-gen-song <project_id>      # tạo bài trên Flow Music, tải các bản (takes) về
/fm-music-video <song_id>      # visualizer, cảnh AI qua Flow Kit, hoặc MV gốc của Flow Music
/fm-pipeline                   # chạy cả chuỗi, tự tiếp tục từ chỗ đang dở
/fm-status, /fm-doctor         # xem tiến độ, chẩn đoán lỗi
```

File xuất ra: `output/<project>/songs/` và `output/<project>/videos/`.

### Ba kiểu music video

| Mode | Kết quả | Cần |
|---|---|---|
| `VISUALIZER` | ảnh cover + sóng âm chạy theo nhạc | ffmpeg |
| `FLOWKIT` | mỗi 8 giây một cảnh AI khớp theo đoạn lời, ghép với bài hát | Flow Kit chạy ở :8100 + tab flow.google.com |
| `NATIVE` | tính năng music video của chính Flow Music (gói trả phí) | recipe `generate_music_video` |

Agent tự chia bài hát thành các cảnh theo thời lượng và section lời
(`POST /api/music-videos` trả về `plan`), Claude viết prompt cho từng cảnh, Flow
Kit sinh video, rồi `POST /api/music-videos/<id>/attach` nối clip và đặt bài hát
lên.

## API chính

| Method | Path | Mô tả |
|---|---|---|
| GET | `/api/status` | extension, tab, ffmpeg, recipe nào đã có, hàng đợi |
| POST/GET/PATCH | `/api/projects` | project |
| POST/GET/PATCH | `/api/songs` | bài hát (prompt, lyrics, bpm, duration_s, vocal…) |
| POST | `/api/songs/{id}/generate` · `/lyrics` · `/extend` · `/cover` | xếp job |
| POST | `/api/songs/import` | nhập file audio có sẵn |
| GET | `/api/songs/{id}/audio` | nghe bản đã chọn |
| GET | `/api/requests` · `/summary` · `/{id}` · POST `/{id}/retry` | hàng đợi |
| POST | `/api/captures/mode` · GET `/api/captures` | capture mode |
| POST | `/api/recipes/from-capture` · `/{name}/render` · `/{name}/run` · `/{name}/test-extract` | recipe |
| POST | `/api/music-videos` · `/{id}/visualizer` · `/{id}/attach` · `/{id}/native` | music video |

## Cấu hình (biến môi trường)

| Biến | Mặc định | |
|---|---|---|
| `API_PORT` / `WS_PORT` | 8300 / 9300 | đổi thì sửa luôn `extension/background.js` + `manifest.json` |
| `GENERATION_MIN_INTERVAL_S` | 5 | khoảng cách tối thiểu giữa 2 lần generate |
| `UNUSUAL_ACTIVITY_COOLDOWN_S` | 180 | tạm dừng khi Google báo bất thường |
| `POLL_INTERVAL_S` / `POLL_TIMEOUT_S` | 8 / 600 | poll job |
| `FLOWKIT_URL` | http://127.0.0.1:8100 | Flow Kit |

## Lưu ý

- Extension chỉ dùng reCAPTCHA công khai của trang; không có cơ chế lách phát
  hiện bot hay telemetry giả. Nếu Google gắn cờ (`UNUSUAL_ACTIVITY`), agent tự
  dừng một lúc — hãy giảm nhịp chứ đừng retry dồn dập.
- Tôn trọng điều khoản của Flow Music và bản quyền: lời gốc, không dùng tên nghệ
  sĩ thật trong prompt, chỉ import audio bạn có quyền.
- Capture chứa response từ tài khoản của bạn — xoá sau khi tạo xong recipe
  (`DELETE /api/captures`).

## Phát triển

```bash
venv/bin/python -m pytest      # 23 test, gồm 1 luồng end-to-end với extension giả
```
