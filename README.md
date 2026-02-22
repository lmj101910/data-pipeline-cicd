# GitLab On-Premises 설치 가이드

Docker Compose를 이용하여 GitLab CE(Community Edition)를 온프레미스 환경에서 구동하는 가이드입니다.


## 사전 요구사항

| 항목 | 최소 사양 |
|------|----------|
| CPU | 2 코어 이상 |
| RAM | 4GB 이상 (권장 8GB) |
| 디스크 | 10GB 이상 |
| OS | Linux, macOS, Windows (WSL2) |
| Docker | 20.10 이상 |
| Docker Compose | v2.0 이상 |

Docker 설치 확인:
```bash
docker --version
docker compose version
```

> Docker Compose v2부터 `version` 필드는 deprecated입니다. `docker-compose.yaml`에 작성하지 않습니다.

---

## 설치 및 실행

### 1. 저장소 클론

```bash
git clone <이 저장소 URL>
cd data-pipeline-cicd
```

### 2. 환경 변수 설정 (이메일 사용 시)

`.env.example`을 복사하여 `.env` 파일을 생성합니다:

```bash
cp .env.example .env
```

`.env` 파일에 실제 값을 입력합니다:

```env
GITLAB_SMTP_USER=your_email@gmail.com
GITLAB_SMTP_PASSWORD=your_app_password
```

> `.env` 파일은 `.gitignore`에 등록되어 있어 git에 커밋되지 않습니다.
> Gmail 앱 비밀번호 발급: [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)

### 3. /etc/hosts 등록 (로컬 접속용)

`gitlab.example.com`은 실제 도메인이 아니므로 로컬 DNS에 등록해야 합니다:

```bash
echo "127.0.0.1 gitlab.example.com" | sudo tee -a /etc/hosts
```

> 실제 서버 배포 시에는 DNS에 등록하거나 `hostname`, `external_url`을 실제 도메인/IP로 변경하세요.

### 4. GitLab 시작

```bash
docker compose up -d
```

처음 실행 시 이미지 다운로드 및 초기화에 **약 5~10분** 소요됩니다.

### 5. 초기화 완료 확인

```bash
docker logs gitlab 2>&1 | grep "Reconfigured"
```

`gitlab Reconfigured!` 메시지가 나타나면 준비 완료입니다.

---

## 포트 구성

| 용도 | 호스트 포트 | 컨테이너 포트 |
|------|------------|--------------|
| HTTP (GitLab 웹) | `8929` | `8929` |
| HTTPS | `8443` | `443` |
| SSH (Git) | `2222` | `22` |

**포트 매핑 주의사항**

`external_url`에 포트를 명시하면 GitLab 내부 nginx가 해당 포트로 리슨합니다.
따라서 `ports` 매핑의 양쪽을 반드시 일치시켜야 합니다:

```yaml
# external_url 'http://gitlab.example.com:8929' 설정 시
ports:
  - '8929:8929'   # ← 양쪽 모두 8929 (불일치 시 접속 불가)
```

---

## 초기 접속

### 웹 브라우저 접속

```
http://gitlab.example.com:8929
```

### 관리자 초기 비밀번호 확인

```bash
docker exec gitlab grep 'Password:' /etc/gitlab/initial_root_password
```

- **아이디:** `root`
- **비밀번호:** 위 명령으로 확인한 임시 비밀번호

> 초기 비밀번호는 **24시간 후 자동 삭제**됩니다. 로그인 후 즉시 변경하세요.
> 경로: 우측 상단 아이콘 → **Edit profile** → **Password**

---

## SSH를 통한 Git 사용

SSH 포트를 `2222`로 설정했으므로, git clone 시 포트를 명시해야 합니다.

```bash
# SSH 방식 (포트 2222 사용)
git clone ssh://git@gitlab.example.com:2222/group/project.git

# 또는 ~/.ssh/config에 설정 추가
Host gitlab.example.com
    HostName gitlab.example.com
    User git
    Port 2222
```

> SSH 포트를 2222로 설정한 이유: 호스트 서버의 22번 포트는 서버 관리용 SSH가 이미 사용 중이므로 충돌을 피하기 위함입니다.

---

## 데이터 저장 위치

Docker Named Volume으로 데이터가 호스트에 영구 저장됩니다.

| 볼륨 이름 | 컨테이너 경로 | 용도 |
|-----------|--------------|------|
| `gitlab_config` | `/etc/gitlab` | GitLab 설정 파일 |
| `gitlab_logs` | `/var/log/gitlab` | 로그 파일 |
| `gitlab_data` | `/var/opt/gitlab` | 저장소, DB 등 데이터 |

볼륨 실제 경로 확인:
```bash
docker volume inspect gitlab_config
```

---

## 주요 관리 명령어

```bash
# GitLab 중지 (데이터 유지)
docker compose down

# 데이터까지 삭제 (초기화)
docker compose down -v

# GitLab 재시작
docker compose restart

# GitLab 업데이트 (최신 이미지로)
docker compose pull
docker compose up -d

# GitLab 컨테이너 내부 접속
docker exec -it gitlab bash

# GitLab 설정 재적용 (설정 변경 후)
docker exec -it gitlab gitlab-ctl reconfigure

# 서비스 상태 확인
docker exec gitlab gitlab-ctl status
```

---

## 백업 및 복구

### 백업 생성

```bash
docker exec -it gitlab gitlab-backup create
```

백업 파일 위치: `/var/opt/gitlab/backups/` (볼륨 `gitlab_data` 내부)

### 백업 복구

```bash
docker exec -it gitlab gitlab-backup restore BACKUP=<timestamp>
```

---

## HTTPS 설정 (선택사항)

Let's Encrypt를 사용하는 경우 `docker-compose.yaml`의 `GITLAB_OMNIBUS_CONFIG`를 수정합니다:

```yaml
external_url 'https://gitlab.example.com'
letsencrypt['enable'] = true
letsencrypt['contact_emails'] = ['admin@example.com']
```

---

## 문제 해결

### 접속이 안 될 때 (Connection reset by peer)

`external_url`의 포트와 `ports` 매핑이 일치하는지 확인합니다:

```bash
# nginx가 실제로 어느 포트에서 리슨하는지 확인
docker exec gitlab gitlab-ctl status

# 포트 바인딩 확인
docker ps --format "table {{.Names}}\t{{.Ports}}"
```

### 502 Bad Gateway

- 초기화가 완료되지 않은 경우입니다. 5~10분 후 다시 접속하세요.
- 아래 명령으로 `Reconfigured!` 메시지 확인:

```bash
docker logs gitlab 2>&1 | grep "Reconfigured"
```

### 메모리 부족

GitLab은 최소 4GB RAM이 필요합니다. 메모리 부족 시 컨테이너가 자동 종료될 수 있습니다.

---

## 참고 링크

- [GitLab Docker 공식 문서](https://docs.gitlab.com/ee/install/docker.html)
- [GitLab CE Docker Hub](https://hub.docker.com/r/gitlab/gitlab-ce)
- [GitLab 관리자 문서](https://docs.gitlab.com/ee/administration/)
