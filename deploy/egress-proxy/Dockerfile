# Canonical-maintained Squid 6.6 for Ubuntu 24.04, pinned by index digest.
FROM ubuntu/squid:6.6-24.04_beta@sha256:6a097f68bae708cedbabd6188d68c7e2e7a38cedd05a176e1cc0ba29e3bbe029

COPY --chown=proxy:proxy squid.conf /etc/squid/squid.conf
COPY --chown=proxy:proxy conf.d/ /etc/squid/webhook-conf.d/

RUN /usr/sbin/squid -k parse -f /etc/squid/squid.conf

USER proxy
EXPOSE 3128
ENTRYPOINT ["/usr/sbin/squid", "--foreground", "-f", "/etc/squid/squid.conf"]
