# Self-contained image: builds pjproject (pjsip) with TLS + the pjsua2 Python
# bindings, then runs the bridge. No other services needed.
FROM debian:12-slim

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    build-essential python3-dev python3-setuptools \
    libssl-dev libopus-dev libasound2-dev swig wget ca-certificates pkg-config \
    && rm -rf /var/lib/apt/lists/*

ARG PJ=2.14.1
WORKDIR /build
RUN wget -q "https://github.com/pjsip/pjproject/archive/refs/tags/${PJ}.tar.gz" -O pj.tgz \
 && tar xzf pj.tgz && mv "pjproject-${PJ}" pjproject
WORKDIR /build/pjproject
RUN printf '#define PJ_HAS_SSL_SOCK 1\n#define PJMEDIA_HAS_VIDEO 0\n' > pjlib/include/pj/config_site.h \
 && ./configure --enable-shared CFLAGS="-fPIC -O2" >/dev/null \
 && make dep >/dev/null 2>&1 && make >/dev/null 2>&1 && make install >/dev/null 2>&1 && ldconfig \
 && cd pjsip-apps/src/swig/python && make >/dev/null 2>&1 && make install >/dev/null 2>&1 \
 && python3 -c "import pjsua2; print('pjsua2 OK')"

WORKDIR /app
COPY sip_open.py bridge.py ./
CMD ["python3", "bridge.py"]
