# desand

여러 LLM 에이전트를 동시에 돌리고, 웹 패널에서 관리하고, 대화 기록을 학습 데이터로 저장하는 작은 Python 에이전트 런타임.

- 에이전트 = `agents/` 안의 파일 하나 (provider, 모델, 시스템 프롬프트, 도구)
- 여러 에이전트에 같은 메시지를 병렬로 보냄, 에이전트마다 메모리 따로
- Anthropic, OpenCode Go, OpenScale, Experiential 지원
- 모든 대화를 OpenAI fine-tuning 형식으로 `trajectories/`에 저장, S3로 업로드
- 로컬 웹 패널 (FastAPI + Jinja2 템플릿)

## 설치

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium   # headless browser for the browser tools
```

사용할 provider의 API 키를 환경변수로 설정:

| provider | 환경변수 | 기본 모델 | 방식 |
|---|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-opus-5-5` | Anthropic Messages |
| `opencode-go` | `OPENCODE_API_KEY` | `kimi-k3` | OpenAI 호환 |
| `opencode-go-anthropic` | `OPENCODE_API_KEY` | `claude-haiku-5-5` | Anthropic 호환 |
| `openscale` | `OPENSCALE_KEY` | 없음 (`MODEL` 지정 필요) | OpenAI 호환 |
| `experiential` | `EXPERIENTIAL_API_KEY` | 없음 (`MODEL` 지정 필요) | OpenAI 호환 |
| `hinoki` | `HINOKI_API_KEY` | 없음 (`MODEL` 지정 필요) | OpenAI 호환 |

## 사용법

### CLI

```bash
.venv/bin/python agent.py --list                          # 에이전트 목록
.venv/bin/python agent.py                                 # agents/default.py 와 대화
.venv/bin/python agent.py -a researcher -a writer         # 두 에이전트에 동시에 대화
.venv/bin/python agent.py -a web "https://example.com 요약해줘"   # 한 번만 실행
```

대화 중 `/reset` 은 메모리 초기화, `exit` 는 종료.

### 웹 패널

```bash
.venv/bin/python server.py   # http://127.0.0.1:8000, 같은 네트워크에선 http://<이 PC IP>:8000
```

- 접속하면 **로그인 페이지**로 이동합니다. 비밀번호는 `PANEL_PASSWORD` 환경 변수 값 (설정 안 하면 실행할 때마다 랜덤 비밀번호가 터미널에 출력). 로그인은 쿠키로 30일 유지되고, 서버를 재시작하면 다시 로그인해야 합니다
- 왼쪽 메뉴:
  - **API 키**: 키 입력 후 저장 (S3 설정값도 여기서, 아래 **S3 업로드** 버튼으로 `trajectories/` 업로드) (점 초록 = 설정됨, 비우고 저장 = 삭제). 서버 메모리에만 저장되어 재시작하면 사라지고, 실행 중인 에이전트는 중지 → 시작 해야 새 키를 씁니다
  - **모델**: 이름 + provider + 모델 ID 로 모델을 저장 (모델 ID 칸을 누르면 provider가 알려주는 목록이 뜸, 키 필요). 에이전트가 쓰는 모델은 삭제할 수 없습니다
  - **에이전트**: 이름 + 저장한 모델 + 시스템 프롬프트 + 도구로 에이전트를 만들고 편집/삭제. `agents/*.py` 파일 에이전트도 목록에 함께 보입니다 (편집은 파일에서)
  - **워크스페이스**: `workspaces/<이름>/` 폴더를 만들거나 삭제 (안의 파일까지 지워짐, 실행 중인 에이전트가 있으면 거부). 에이전트 파일 도구는 자기 workspace 밖(다른 workspace, 상위 폴더, 심볼릭 링크 너머)에 접근할 수 없습니다. 이름은 글자·숫자·`-` `_` `.` 만
  - **대화**: **+ 에이전트 시작** 에서 에이전트, 워크스페이스, 모델(기본값 또는 저장한 모델)을 골라 시작
- 패널에서 만든 모델과 에이전트는 실행 폴더의 `panel.json` 에 저장됩니다 (Docker 에서는 `/work/panel.json`)
- 카드의 체크박스로 메시지 받을 에이전트 선택 → 아래 입력창에서 전송 (Enter 전송, Shift+Enter 줄바꿈)
- 카드마다 상태(작업 중/대기), 대화, 도구 호출, 에러 표시
- 초기화 로 메모리 초기화, 중지 로 종료

서버를 재시작하면 실행 중이던 에이전트와 메모리는 사라집니다 (`trajectories/` 기록은 남음).

### Docker

`main` 푸시와 `v*` 태그마다 GitHub Actions가 이미지를 빌드해 GHCR에 올립니다 (`linux/amd64`, `linux/arm64`).

```bash
docker run --rm -p 8000:8000 \
  -e ANTHROPIC_API_KEY -e PANEL_PASSWORD \
  -v "$PWD:/work" \
  -v "$PWD/trajectories:/app/trajectories" \
  ghcr.io/hiteyy/desand:latest
```

- `/work`: workspace 들이 `/work/workspaces/<이름>/` 에 생성됨 (CLI `agent.py` 는 `/work` 전체)
- `/app/trajectories`: 학습 데이터 저장 위치
- 이미지에 Playwright + 헤드리스 Chromium 이 포함되어 있어 브라우저 도구를 바로 쓸 수 있습니다
- `-p 8000:8000` 은 외부에서도 접속 가능. 이 PC에서만 쓰려면 `-p 127.0.0.1:8000:8000`. 인증은 비밀번호 하나뿐이고 HTTPS가 아닙니다.
- 태그: `latest`(main), `main`, `sha-<커밋>`, `v1.2.3` 태그 푸시 시 `1.2.3`·`1.2`

## 에이전트 만들기

웹 패널의 **에이전트** 메뉴에서 만들거나, `agents/<이름>.py` 파일 하나를 만들면 됩니다. 파일의 모든 항목은 선택:

```python
from core import list_files, read_file

PROVIDER = "openscale"            # 기본 "anthropic"
MODEL = "..."                     # 기본: provider 기본 모델
SYSTEM = "You are ..."            # 기본: 범용 시스템 프롬프트
TOOLS = [list_files, read_file]   # 기본: 로컬 도구 전체
```

포함된 예시:

| 파일 | provider | 도구 |
|---|---|---|
| `default.py` | anthropic | 로컬 도구 전체 |
| `researcher.py` | opencode-go (`kimi-k3`) | 읽기 전용 (`list_files`, `read_file`) |
| `writer.py` | anthropic | 로컬 도구 전체 |
| `web.py` | anthropic | 웹 검색/가져오기 + 파일 도구 |

### 도구

| 도구 | 설명 |
|---|---|
| `get_time` | 현재 시각 |
| `list_files`, `read_file`, `write_file` | 실행 폴더 안의 파일만 접근 (밖은 거부) |
| `browser_open`, `browser_click`, `browser_fill`, `browser_text`, `browser_screenshot` | 헤드리스 Chromium 으로 페이지 열기/클릭/입력/텍스트/스크린샷. 스크린샷은 실행 폴더에 저장 |
| `WEB_TOOLS` | `web_search`, `web_fetch`. `PROVIDER = "anthropic"`이면 Anthropic 서버 도구(턴당 각 5회), 그 외 provider는 같은 이름의 로컬 구현(DuckDuckGo 검색, URL 텍스트 가져오기) |

직접 도구를 추가하려면 `anthropic`의 `@beta_tool` 함수를 만들어 `TOOLS`에 넣으면 됩니다. 함수 시그니처와 docstring에서 스키마가 자동 생성되고, OpenAI 호환 provider에도 같은 도구가 쓰입니다.

## 학습 데이터

성공한 턴마다 `trajectories/<세션>-<에이전트>.jsonl` 에 대화 전체가 저장됩니다 (실패한 턴은 저장 안 됨).

- 형식: OpenAI fine-tuning 채팅 형식 JSONL. 파일 하나 = 한 줄 = 예제 하나 (`{"messages": [...], "tools": [...]}`), provider 상관없이 동일
- 포함: 시스템 프롬프트, 사용자 메시지, 도구 호출과 결과, 응답
- OpenAI가 받지 않는 키는 제외 (provider가 붙이는 `reasoning_content`, `refusal` 등, 에이전트/provider/모델 메타데이터)
- Anthropic thinking 블록은 제외

하나의 JSONL로 합치기:

```bash
cat trajectories/*.jsonl > train.jsonl
```

### S3 업로드

`trajectories/*.jsonl` 을 `s3://$S3_BUCKET/$S3_PREFIX/` 에 올립니다 (같은 이름은 덮어씀). 웹 패널 **API 키** 페이지의 **S3 업로드** 버튼, 또는:

```bash
.venv/bin/python agent.py --upload-s3
```

| 환경변수 | 설명 |
|---|---|
| `S3_BUCKET` | 버킷 이름 (필수) |
| `S3_PREFIX` | 키 앞부분, 기본 `trajectories` (빈 값 = 버킷 루트) |
| `S3_ENDPOINT_URL` | Cloudflare R2, MinIO 등 S3 호환 저장소 주소 (AWS면 비움) |
| `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | 자격 증명. 비우면 boto3 기본 경로(`~/.aws`, IAM 역할) 사용 |

필요 권한: `s3:PutObject`. 패널에서 넣은 값은 API 키처럼 서버 메모리에만 저장됩니다.

주의:
- 각 provider의 이용약관을 확인하세요. Anthropic 등은 출력물로 경쟁 모델을 학습하는 것을 금지합니다.
- 도구 결과에 읽은 파일 내용이 들어가므로 비밀값이 섞일 수 있습니다. `trajectories/`는 `.gitignore`에 포함되어 있습니다.

## 보안

- 웹 패널은 기본으로 `0.0.0.0` (모든 네트워크 인터페이스)에 열리고 로그인 비밀번호(세션 쿠키)로 보호됩니다. 평문 HTTP라 같은 네트워크에서 비밀번호를 엿볼 수 있으니, 인터넷에 노출할 땐 HTTPS 리버스 프록시를 앞에 두세요. 이 PC에서만 쓰려면 `HOST=127.0.0.1`.
- 다른 웹사이트가 패널을 조작하지 못하도록 POST는 JSON Content-Type만 받습니다.
- 파일 도구는 실행 폴더 밖 경로를 거부합니다.
- S3 업로드는 사람이 버튼/CLI로만 실행합니다 (에이전트 도구 아님). 업로드되는 기록엔 도구로 읽은 파일 내용이 들어 있으니 버킷을 공개로 두지 마세요.
- 브라우저 도구는 임의의 URL을 열 수 있으므로 페이지 내용에 섞인 지시문(prompt injection)에 주의하세요.
- `web` 에이전트는 웹 페이지 내용에 섞인 지시문(prompt injection)에 속아 파일을 쓸 수 있습니다. 걱정되면 `agents/web.py`에서 `write_file`을 빼세요.

## 구조

```
core.py       도구, provider, 에이전트 실행 루프, 기록 저장
agent.py      CLI (여러 에이전트 병렬 실행)
server.py     웹 패널 (FastAPI)
templates/    패널 HTML 템플릿 (Jinja2: 로그인, 대화, 에이전트, 모델, 워크스페이스, API 키)
static/       패널 CSS, JS
agents/       에이전트 정의 파일
```
