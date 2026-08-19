chflags -R nohidden .venv
python -m pip install --force-reinstall --no-deps -e ".[dev,gx-postgres]"
rehash

python -c "import cli; print(cli.__file__)"
tradingagents-gx --help

tradingagents-gx --env-file .env.postgres-hosted \
  media collect --once --ticker PNJ

# Chỉ dùng cùng ngày nếu collector đã chạy trước cutoff 15:00. Nếu đây là lần
# đầu collect và đã quá 15:00, dữ liệu mới chỉ hợp lệ cho phiên hoàn tất kế tiếp.
# Session/báo cáo cũ không tự cập nhật sau collect.

# tradingagents-gx --env-file .env.postgres-hosted full \
#  --ticker PNJ \
#  --date 2026-08-18


tradingagents-gx --env-file .env.postgres-hosted full \
  --ticker PNJ \
  --as-of-now