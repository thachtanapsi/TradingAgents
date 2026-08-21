# Giao diện local TradingAgents GX

UI local cung cấp form nhập ticker, chế độ đóng cửa hoặc `as-of-now`, tiến trình
từng stage và toàn bộ kết quả Research. Trang chạy phân tích và trang lịch sử
được tách riêng để polling của run hiện tại không ảnh hưởng viewer read-only.
UI không gửi lệnh giao dịch thật.

## Cài và chạy

Sau khi cập nhật source, cài lại editable package để có entrypoint mới:

```bash
cd /Users/thachtan/Documents/source/APG/TradingAgents
source .venv/bin/activate
python -m pip install -e ".[gx-postgres,fireant,vn-media,vn-macro]"

tradingagents-gx-ui --env-file .env.postgres-hosted
```

Nếu macOS báo `ModuleNotFoundError: No module named 'cli'` sau khi cài editable,
gỡ cờ hidden khỏi virtualenv rồi cài lại:

```bash
chflags -R nohidden .venv
python -m pip install --no-build-isolation -e ".[gx-postgres,fireant,vn-media,vn-macro]"
```

Trình duyệt mở `http://127.0.0.1:8765`. Có thể chọn port khác hoặc không tự mở
trình duyệt:

```bash
tradingagents-gx-ui --env-file .env.postgres-hosted --port 8899 --no-open
```

UI chỉ bind vào loopback `127.0.0.1`; không có tùy chọn expose ra LAN. Mỗi lần
khởi động tạo một UI token mới và kiểm tra Host/Origin cho request chạy pipeline.
Không có key, DSN, raw FireAnt post, raw RSS hay author identity nào được trả về
browser.

## Cách dùng

1. Nhập ticker như `FPT`, `HPG` hoặc `CTG`.
2. Chọn **Đóng cửa 15:00** và một ngày, hoặc **Live · as-of-now**.
   Trước 15:00, close của ngày hiện tại chưa hoàn tất nên UI yêu cầu chọn ngày
   trước đó hoặc dùng live mode; ngày tương lai luôn bị từ chối phía server.
3. Với live mode, có thể bật thu thập CafeF/VnExpress và FireAnt trước khi khóa
   cutoff. Ticker truyền từ UI không cần có sẵn trong watchlist.
4. Nếu Quick hoặc Deep dùng hosted provider, xác nhận khả năng phát sinh chi phí.
5. Bấm **Chạy phân tích** đúng một lần. Refresh không tạo run mới; UI khôi phục
   job hiện tại trong cùng phiên trình duyệt và tiếp tục hiển thị tiến trình.

Trong khi pipeline đang chạy, có thể mở **Lịch sử Research**. Backend tiếp tục
xử lý run, còn trang lịch sử không poll job và không bị render lại. Quay về
**Trang Research** để tiếp tục xem tiến trình đã lưu trong phiên trình duyệt.

Các job được thực thi tuần tự vì cấu hình dataflow là process-global. Khi một job
đang chạy, UI từ chối tạo thêm job để tránh double-click và phát sinh chi phí lặp.

## Ý nghĩa các card

- **Khuyến nghị** chỉ lấy từ trường `Rating` của Portfolio Manager. `Overweight`
  được hiển thị nhóm BUY và `Underweight` thuộc nhóm SELL, đồng thời vẫn hiện
  rating gốc.
- **Giá mục tiêu** chỉ hiển thị khi Portfolio Manager trả đủ contract
  `Status=Available`, giá, currency và cơ sở định giá. Giá được kiểm tra với
  completed daily close đã đóng băng tại cutoff; target GX phải dùng đơn vị VND
  đầy đủ. Hệ thống từ chối chứ không tự đổi `63.3` thành `63,300`. Khi không đủ
  dữ liệu, card hiển thị `Unavailable` kèm lý do của Portfolio Manager.
- Tăng `TRADINGAGENTS_MAX_DEBATE_ROUNDS` hoặc
  `TRADINGAGENTS_MAX_RISK_DISCUSS_ROUNDS` làm cuộc tranh luận dài hơn nhưng không
  bảo đảm có target. Target chỉ xuất hiện khi đủ giá tham chiếu PIT và bằng chứng
  định giá hợp lệ.
- **Confidence** và **Mức rủi ro** hiển thị `Unavailable` vì schema quyết định v1
  chưa có hai trường này. UI không đổi sentiment confidence thành độ tin cậy của
  quyết định và không suy diễn risk từ hành động.
- Viewer không tải lại chart khi mở lịch sử vì dữ liệu được truy vấn ở thời điểm
  xem có thể khác cutoff của research đã lưu.

Kết quả đầy đủ vẫn được lưu atomically trong `session.json` theo cơ chế
`TradingAgentsStageRunner`; browser chỉ nhận các report field đã allowlist.

## Trang Research và trang Lịch sử

Hai chức năng dùng hai URL độc lập:

```text
Research: http://127.0.0.1:8765/
Lịch sử:  http://127.0.0.1:8765/history
```

Trang Research chứa form, tiến độ và kết quả của run hiện tại. Trang lịch sử
đọc các `session.json` đã lưu trong `TRADINGAGENTS_STAGE_RUNS_DIR` và không gọi
lại GX, LLM, báo chí, FireAnt hay NSO/SBV.

Có thể tìm theo ticker hoặc Run ID, lọc ngày, chế độ close/live và trạng thái,
sau đó xem decision, technical, fundamental, news/sentiment, kế hoạch, tranh
luận và provenance.

URL có fragment `#<history-id>` để bookmark đúng một phiên research. Nút làm
mới nhận các session vừa được CLI hoặc UI ghi xong. Viewer chỉ trả dữ liệu đã
allowlist, không hiển thị filesystem path, raw JSON, key, DSN hay archive ID;
v1 không hỗ trợ resume, xóa, tải xuống hoặc chạy lại từ trang lịch sử.
