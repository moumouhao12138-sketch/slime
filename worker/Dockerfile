ARG BASE_IMAGE=kalilinux/kali-rolling
FROM ${BASE_IMAGE}

ARG KALI_APT_MIRROR=https://mirrors.ustc.edu.cn/kali/
ARG KALI_APT_VERIFY_PEER=true
ARG INSTALL_NATIVE_AGENTS=true
ARG INSTALL_REFERENCE_ASSETS=false
ARG CODEX_VERSION=0.144.6
ARG CLAUDE_CODE_VERSION=2.1.112
ARG PI_AGENT_VERSION=0.73.1

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

RUN --mount=type=cache,id=slime-cairn-apt-archives,target=/var/cache/apt/archives,sharing=locked \
    --mount=type=cache,id=slime-cairn-apt-lists,target=/var/lib/apt/lists,sharing=locked \
    sed -i "s|^URIs: .*|URIs: ${KALI_APT_MIRROR}|" /etc/apt/sources.list.d/kali.sources \
    && if [ "${KALI_APT_VERIFY_PEER}" = "false" ]; then printf 'Acquire::https::Verify-Peer "false";\nAcquire::https::Verify-Host "false";\n' > /etc/apt/apt.conf.d/99slime-cairn-insecure-https; fi \
    && printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' > /etc/apt/apt.conf.d/99slime-cairn-retries \
    && apt-get update -o Acquire::Retries=5 \
    && apt-get install -y --fix-missing \
        ca-certificates curl wget git jq ripgrep fd-find file binutils openssl \
        python3 python3-pip python3-venv nodejs npm \
        tmux procps iproute2 net-tools dnsutils netcat-openbsd socat proxychains4 \
        unzip p7zip-full make gcc gdb gdbserver checksec strace ltrace \
        nmap masscan nikto sqlmap hydra john hashcat smbclient redis-tools \
        postgresql-client whois ffuf feroxbuster gobuster dirsearch nuclei

COPY scripts/build_tool_manifest.py /usr/local/bin/build_tool_manifest.py
COPY scripts/fetch_reference_asset.sh /usr/local/bin/fetch_reference_asset
RUN if [ "${INSTALL_NATIVE_AGENTS}" = "true" ]; then \
        npm install -g \
            "@openai/codex@${CODEX_VERSION}" \
            "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
            "@mariozechner/pi-coding-agent@${PI_AGENT_VERSION}"; \
    fi

RUN chmod +x /usr/local/bin/fetch_reference_asset \
    && mkdir -p /opt/slime-cairn/pocs /opt/slime-cairn/tools /opt/slime-cairn/knowledges

RUN if [ "${INSTALL_REFERENCE_ASSETS}" = "true" ]; then \
        fetch_reference_asset https://codeload.github.com/swisskyrepo/PayloadsAllTheThings/tar.gz/refs/heads/master /opt/slime-cairn/knowledges/PayloadsAllTheThings; \
    fi

RUN if [ "${INSTALL_REFERENCE_ASSETS}" = "true" ]; then \
        fetch_reference_asset https://codeload.github.com/HackTricks-wiki/hacktricks/tar.gz/refs/heads/master /opt/slime-cairn/knowledges/hacktricks; \
    fi

RUN if [ "${INSTALL_REFERENCE_ASSETS}" = "true" ]; then \
        fetch_reference_asset https://codeload.github.com/iSee857/CVE-PoC/tar.gz/refs/heads/main /opt/slime-cairn/pocs/CVE-PoC; \
    fi

RUN if [ "${INSTALL_REFERENCE_ASSETS}" = "true" ]; then \
        fetch_reference_asset https://codeload.github.com/ticarpi/jwt_tool/tar.gz/refs/heads/master /opt/slime-cairn/tools/jwt_tool; \
    fi

RUN (setcap -r /usr/lib/nmap/nmap || true) \
    && python3 /usr/local/bin/build_tool_manifest.py /opt/slime-cairn/tools.json \
    && groupadd --gid 65532 slime \
    && useradd --uid 65532 --gid 65532 --home-dir /workspace --no-create-home slime \
    && mkdir -p /workspace \
    && chown 65532:65532 /workspace \
    && chmod -R a+rX /opt/slime-cairn

WORKDIR /workspace
USER 65532:65532

ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    PLAYWRIGHT_MCP_BROWSER=chromium

ENTRYPOINT []
CMD ["sleep", "infinity"]
