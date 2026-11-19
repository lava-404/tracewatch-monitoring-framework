FROM grafana/grafana:11.3.0

COPY provisioning /etc/grafana/provisioning
COPY dashboards /var/lib/grafana/dashboards

ENV GF_USERS_ALLOW_SIGN_UP=false
ENV GF_ANALYTICS_REPORTING_ENABLED=false