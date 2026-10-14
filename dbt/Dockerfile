FROM python:3.12-slim-bookworm

# MSSQL(dbt-sqlserver/pyodbc)이 필요로 하는 ODBC 드라이버 설치
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg unixodbc unixodbc-dev gcc g++ git \
    && curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -sSL -o /etc/apt/sources.list.d/mssql-release.list https://packages.microsoft.com/config/debian/12/prod.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 mssql-tools18 \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="$PATH:/opt/mssql-tools18/bin"

WORKDIR /dbt

COPY requirements.txt /dbt/requirements.txt
RUN pip install --no-cache-dir -r /dbt/requirements.txt

ENTRYPOINT ["tail", "-f", "/dev/null"]
