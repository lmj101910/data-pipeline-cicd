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

---

## 설치 및 실행

### 1. 저장소 클론 (이 설정 파일 가져오기)

```bash
git clone <이 저장소 URL>
cd gitlab_onpremis
```

### 2. 도메인 설정

[docker-compose.yaml](docker-compose.yaml) 파일에서 `gitlab.example.com`을 실제 도메인 또는 IP로 변경합니다.

```yaml
hostname: 'your-domain.com'       # 실제 도메인 또는 서버 IP
external_url 'http://your-domain.com'   # 외부 접속 URL
```

> **로컬 테스트 시** `gitlab.example.com` 대신 서버 IP(예: `192.168.1.100`)를 사용하세요.

### 3. GitLab 시작

```bash
docker compose up -d
```

처음 실행 시 이미지 다운로드 및 초기화에 **약 3~5분** 소요됩니다.

### 4. 실행 상태 확인

```bash
# 컨테이너 상태 확인
docker compose ps

# 초기화 로그 확인 (healthy 상태가 될 때까지 대기)
docker compose logs -f gitlab
```

`gitlab Reconfigured!` 메시지가 나타나면 준비 완료입니다.

---

## 초기 접속

### 웹 브라우저 접속

```
http://gitlab.example.com  (또는 설정한 IP/도메인)
```

### 관리자 초기 비밀번호 확인

```bash
docker exec -it gitlab grep 'Password:' /etc/gitlab/initial_root_password
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
# GitLab 중지
docker compose down

# GitLab 재시작
docker compose restart

# GitLab 업데이트 (최신 이미지로)
docker compose pull
docker compose up -d

# GitLab 컨테이너 내부 접속
docker exec -it gitlab bash

# GitLab 설정 재적용 (설정 변경 후)
docker exec -it gitlab gitlab-ctl reconfigure
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

### 502 Bad Gateway
- 초기화가 완료되지 않은 경우입니다. 3~5분 후 다시 접속하세요.
- `docker compose logs -f gitlab` 로그에서 `Reconfigured!` 확인

### 포트 충돌
- 80, 443, 2222 포트가 이미 사용 중인 경우 `docker-compose.yaml`에서 포트 변경:
```yaml
ports:
  - '8080:80'   # 호스트 포트:컨테이너 포트
  - '8443:443'
  - '2222:22'
```

### 메모리 부족
- GitLab은 최소 4GB RAM이 필요합니다. 메모리 부족 시 컨테이너가 자동 종료될 수 있습니다.

---

## 참고 링크

- [GitLab Docker 공식 문서](https://docs.gitlab.com/ee/install/docker.html)
- [GitLab CE Docker Hub](https://hub.docker.com/r/gitlab/gitlab-ce)
- [GitLab 관리자 문서](https://docs.gitlab.com/ee/administration/)
