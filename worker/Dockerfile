ARG BASE_IMAGE=kalilinux/kali-rolling
FROM ${BASE_IMAGE}

ARG KALI_APT_MIRROR=http://http.kali.org/kali
ARG KALI_APT_VERIFY_PEER=true
ARG INSTALL_NATIVE_AGENTS=true
ARG INSTALL_REFERENCE_ASSETS=true
ARG CODEX_VERSION=0.144.6
ARG CLAUDE_CODE_VERSION=2.1.112
ARG PI_AGENT_VERSION=0.73.1

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HOME=/home/kali \
    TZ=Asia/Shanghai

RUN --mount=type=cache,id=slime-cairn-apt-archives,target=/var/cache/apt/archives,sharing=locked \
    --mount=type=cache,id=slime-cairn-apt-lists,target=/var/lib/apt/lists,sharing=locked \
    sed -i "s|^URIs: .*|URIs: ${KALI_APT_MIRROR}|" /etc/apt/sources.list.d/kali.sources \
    && if [ "${KALI_APT_VERIFY_PEER}" = "false" ]; then printf 'Acquire::https::Verify-Peer "false";\nAcquire::https::Verify-Host "false";\n' > /etc/apt/apt.conf.d/99slime-cairn-insecure-https; fi \
    && printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' > /etc/apt/apt.conf.d/99slime-cairn-retries \
    && apt-get update -o Acquire::Retries=5 \
    && apt-get install -y --fix-missing \
        kali-linux-headless \
        bsdextrautils npm jq iputils-ping sshpass ncat rlwrap \
        bloodyad coercer enum4linux-ng pwncat chisel-common-binaries \
        dirsearch yq krb5-user gitleaks naabu nikto netexec adb \
        ca-certificates curl wget git ripgrep fd-find file binutils openssl \
        python3 python3-pip python3-venv nodejs sudo tmux procps \
        iproute2 net-tools dnsutils netcat-openbsd socat proxychains4 \
        unzip p7zip-full make gcc gdb gdbserver checksec strace ltrace \
        nmap masscan sqlmap hydra john hashcat smbclient redis-tools \
        postgresql-client whois ffuf feroxbuster gobuster nuclei \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 kali \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash kali \
    && printf 'kali ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/kali \
    && chmod 0440 /etc/sudoers.d/kali

COPY scripts/fetch_reference_asset.sh /usr/local/bin/fetch_reference_asset

RUN chmod +x /usr/local/bin/fetch_reference_asset \
    && pip3 install --break-system-packages pwntools pymongo \
    && if [ "${INSTALL_NATIVE_AGENTS}" = "true" ]; then \
        npm install -g \
            "@openai/codex@${CODEX_VERSION}" \
            "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
            "@mariozechner/pi-coding-agent@${PI_AGENT_VERSION}"; \
    fi \
    && npm install -g @playwright/cli@latest

RUN mkdir -p /home/kali/.local/nuclei-templates /home/kali/knowledges /home/kali/tools /home/kali/pocs \
    && git clone --depth 1 https://github.com/projectdiscovery/nuclei-templates.git /home/kali/.local/nuclei-templates \
    && cd /tmp \
    && wget -q https://github.com/projectdiscovery/katana/releases/download/v1.5.0/katana_1.5.0_linux_amd64.zip -O katana.zip \
    && wget -q https://github.com/projectdiscovery/nuclei/releases/download/v3.7.1/nuclei_3.7.1_linux_amd64.zip -O nuclei.zip \
    && wget -q https://github.com/hahwul/dalfox/releases/download/v2.12.0/dalfox-linux-amd64.tar.gz -O dalfox.tar.gz \
    && wget -q https://github.com/BishopFox/cloudfox/releases/download/v2.0.1/cloudfox-linux-amd64.zip -O cloudfox.zip \
    && wget -q https://github.com/ropnop/kerbrute/releases/download/v1.0.3/kerbrute_linux_amd64 -O kerbrute \
    && unzip -q katana.zip katana \
    && unzip -q nuclei.zip nuclei \
    && tar -xzf dalfox.tar.gz \
    && unzip -q cloudfox.zip cloudfox/cloudfox \
    && install -m 0755 katana nuclei dalfox-linux-amd64 kerbrute /usr/local/bin/ \
    && install -m 0755 cloudfox/cloudfox /usr/local/bin/cloudfox \
    && rm -rf /tmp/*

RUN if [ "${INSTALL_REFERENCE_ASSETS}" = "true" ]; then \
        git clone --depth 1 https://github.com/swisskyrepo/PayloadsAllTheThings.git /home/kali/knowledges/PayloadsAllTheThings; \
        git clone --depth 1 https://github.com/swisskyrepo/InternalAllTheThings.git /home/kali/knowledges/InternalAllTheThings; \
        git clone --depth 1 https://github.com/HackTricks-wiki/hacktricks.git /home/kali/knowledges/hacktricks; \
        git clone --depth 1 https://github.com/HackTricks-wiki/hacktricks-cloud.git /home/kali/knowledges/hacktricks-cloud; \
        wget -q https://github.com/frohoff/ysoserial/releases/download/v0.0.6/ysoserial-all.jar -O /home/kali/tools/ysoserial.jar; \
        git clone --depth 1 https://github.com/ticarpi/jwt_tool.git /home/kali/tools/jwt_tool; \
        pip3 install --break-system-packages -r /home/kali/tools/jwt_tool/requirements.txt; \
        git clone --depth 1 https://github.com/IOActive/jdwp-shellifier.git /home/kali/tools/jdwp-shellifier; \
        git clone --depth 1 https://github.com/iSee857/CVE-PoC.git /home/kali/pocs/CVE-PoC; \
        git clone --depth 1 https://github.com/zhzyker/exphub.git /home/kali/pocs/exphub; \
        git clone --depth 1 https://github.com/passwa11/2023Hvv_.git /home/kali/pocs/2023Hvv_; \
        git clone --depth 1 https://github.com/Threekiii/Awesome-POC.git /home/kali/pocs/Awesome-POC; \
        git clone --depth 1 https://github.com/vulhub/vulhub.git /home/kali/pocs/vulhub; \
    fi

COPY scripts/build_tool_manifest.py /usr/local/bin/build_tool_manifest.py

RUN install -d -o 1000 -g 1000 \
        /home/kali/.config/nuclei /home/kali/.cache /home/kali/.cache/ms-playwright /workspace \
    && mkdir -p /opt/slime-cairn \
    && printf 'disable-update-check: true\nupdate-template-dir: /home/kali/.local/nuclei-templates\n' > /home/kali/.config/nuclei/config.yaml \
    && ln -s /home/kali/pocs /opt/slime-cairn/pocs \
    && ln -s /home/kali/tools /opt/slime-cairn/tools \
    && ln -s /home/kali/knowledges /opt/slime-cairn/knowledges \
    && ln -s /workspace /home/kali/workspace \
    && printf '%s\n' 'PS1="${PS1%\\n*} \[\033[0;36m\]\D{%y-%m-%d} - \t\[\033[;32m\]\n${PS1##*\\n}"' >> /home/kali/.bashrc \
    && rm -rf /usr/lib/llvm-*/build \
    && chmod o+rx /usr/lib/mysql/plugin/auth_pam_tool_dir /etc/ssl/private \
    && chown -R 1000:1000 /home/kali/.config /home/kali/.cache /workspace \
    && runuser -u kali -- env HOME=/home/kali \
        python3 /usr/local/bin/build_tool_manifest.py /tmp/slime-tools.json \
    && install -m 0644 /tmp/slime-tools.json /opt/slime-cairn/tools.json \
    && rm /tmp/slime-tools.json \
    && test ! -e /home/kali/.codex \
    && (setcap -r /usr/lib/nmap/nmap || true)

USER kali
RUN cd /tmp && playwright-cli install

USER root
RUN ffmpeg_path="$(find /home/kali/.cache/ms-playwright -type f -name ffmpeg-linux -print -quit)" \
    && test -n "${ffmpeg_path}" \
    && ln -s "${ffmpeg_path}" /usr/local/bin/ffmpeg \
    && ffmpeg_version="$(ffmpeg -version | sed -n '1p')" \
    && jq \
        --arg path /usr/local/bin/ffmpeg \
        --arg version "${ffmpeg_version}" \
        '[.[] | select(.name != "ffmpeg")] + [{"name":"ffmpeg","path":$path,"version":$version,"category":"media","interactive":false,"required_capabilities":[],"available":true}]' \
        /opt/slime-cairn/tools.json > /tmp/slime-tools.json \
    && install -m 0644 /tmp/slime-tools.json /opt/slime-cairn/tools.json \
    && rm /tmp/slime-tools.json \
    && test ! -e /home/kali/.codex

USER kali
WORKDIR /workspace

ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    PLAYWRIGHT_MCP_BROWSER=chromium

ENTRYPOINT []
CMD ["sleep", "infinity"]
