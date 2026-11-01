# Cài workflow trên Windows khác

Đây là ZIP mã nguồn với bộ cài runtime online có khóa hash; không phải ứng dụng chạy offline ngay sau khi giải nén. Không chứa phim, cache giọng, dự án CapCut hoặc kết quả nghiệm thu của phim trước.

## Điều kiện của máy

- Windows x64, CPython 3.12 x64 có `venv` và `pip`; cài từ [Python chính thức](https://www.python.org/downloads/windows/) nếu chưa có. Có thể truyền `-PythonPath` để chọn chính xác Python.
- NVIDIA GPU/driver chạy được CUDA 11.8, đủ RAM/VRAM và khoảng 30 GB trống cho bộ runtime, bản tải, model và log cài đặt. Setup lấy Microsoft VC runtime từ Appx chính thức vào thư mục riêng, không cài Appx vào Windows. Dung lượng làm phim được tính riêng theo toàn bộ phim.
- CapCut desktop đã cài. Nhận diện Chinese Auto captions và lưu draft vẫn do người dùng thực hiện trong CapCut. Thư viện pyCapCut chỉ chuẩn bị/đọc draft.
- Internet để tải archive/model và dùng dịch vụ Ava/nguồn phim. Dịch vụ từ chối hoặc không có exact HEVC1080p30 sẽ dừng; không đổi backend hoặc chất lượng.

## Cài đặt

Giải nén source vào thư mục mới, giữ nguyên tên các thư mục bên trong. Mở PowerShell tại thư mục này:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\workflow.ps1 setup -PythonPath 'C:\Python312\python.exe' -RuntimeRoot 'D:\WorkflowRuntime\1.1.0' -Config 'D:\WorkflowRuntime\1.1.0\config\local.json'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\workflow.ps1 doctor -Config 'D:\WorkflowRuntime\1.1.0\config\local.json' --online
```

`RuntimeRoot` phải nằm ngoài thư mục source. Mỗi role có venv riêng; không cài package vào Python gọi setup, không sửa PATH hệ thống, không sửa runtime của dự án khác. Python nền, driver NVIDIA và ứng dụng CapCut là điều kiện của máy, không nằm trong ZIP.

`-ExecutionPolicy Bypass` chỉ áp dụng cho tiến trình PowerShell này; không đổi execution policy hệ thống.

Setup tải Node24.22.1, FFmpeg9.0.2, cuDNN8.9.7.29, OCR mobile models theo commit và checkpoint MVSEP đã khóa SHA256. Package Python dùng toàn bộ dependency closure đã resolve khi tạo release, cài với `--require-hashes --no-deps --no-build-isolation`; build tooling cũng có khóa hash riêng. pyCapCut lấy từ commit `ec61408d9f039c4045f946370bb35746b89b2479`, không dùng editable install trỏ về máy tác giả. Model/runtime tải xuống nằm ngoài ZIP.

Trước khi tải các package/model lớn, setup dùng CUDA Driver API kiểm tra `nvcuda.dll`, ít nhất một GPU và driver API11.8 trở lên. Không đặt ngưỡng VRAM đoán trước; smoke inference thật quyết định máy có chạy được profile hay không. Lỗi quyền CUPTI/profiling phải được xử lý trong cấu hình NVIDIA của máy, không được bỏ qua bằng chứng GPU.

Archive sai hash, package khác version, thiếu DLL/GPU hoặc smoke thất bại đều làm setup trả mã lỗi và giữ log/attempt. Không tự thay version, bỏ bước GPU hoặc ghi READY. Chạy lại cùng runtime chỉ được dùng các payload còn nguyên hash; nếu đã sửa payload/khóa, chọn RuntimeRoot mới. Không xóa history để chạy tiếp.

## Xác minh

Doctor đọc hash runtime/tool/model, import thư viện thật trong từng Python, thử thao tác GPU FP32 của Paddle/Torch, kiểm native MediaInfo, Node/axios, FFmpeg filters/encoders và khóa GPU dùng chung toàn máy. Doctor offline đạt chỉ báo `ENGINE_IMPORTS_PASS_SMOKE_REQUIRED`.

Setup luôn chạy smoke thật: media tổng hợp, inference OCR GPU, MVSEP GPU FP32/CUPTI và một câu Ava online. Chỉ có `PASS` từ smoke thực đo mới được coi là xác minh engine thành công. Không suy ra phim đã dịch đúng, tên đã duyệt hoặc audio đã nghe từ smoke này. `doctor --online` chạy lại smoke; nguồn phim cụ thể được kiểm trong preflight/download của run mới.

Tất cả kết quả cài ở `RuntimeRoot/setup-attempts/<attempt>/`; log có đường dẫn lệnh và lỗi cần xử lý. Config chứa path cục bộ và không phải dữ liệu phát hành. Nếu di chuyển thư mục source, tạo lại config bằng setup trong vị trí mới trước khi tạo run mới; không di chuyển/viết lại run đã khóa.

## Nguồn và notices

Artifact URL/hash nằm trong `config/downloads.lock.json`; Python archives trong `config/locks/*.archives.json` và `*.requirements.txt`. Nguồn gồm [Node](https://nodejs.org/dist/v24.22.1/), [FFmpeg Gyan](https://www.gyan.dev/ffmpeg/builds/), [Paddle](https://www.paddlepaddle.org.cn/packages/stable/cu118/paddlepaddle-gpu/), [NVIDIA cuDNN](https://developer.download.nvidia.com/compute/cudnn/redist/cudnn/windows-x86_64/), [Paddle models](https://huggingface.co/PaddlePaddle), [MVSEP](https://github.com/ZFTurbo/MVSEP-CDX23-Cinematic-Sound-Demixing), [pyCapCut](https://github.com/GuanYixuan/pyCapCut).

Giữ `NOTICE.md` và các license kèm source adapter/core. ZIP không phân phối lại Python, FFmpeg, CUDA/cuDNN, model hoặc font Arial; setup tải đúng nguồn và lưu provenance. Không gán một license chung cho mọi thành phần; quyền đối với phim/branding không được suy ra từ license phần mềm.
