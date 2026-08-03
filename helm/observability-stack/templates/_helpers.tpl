{{/* Naming and label helpers. Kept boring and predictable on purpose —
     `kubectl get pods -l app.kubernetes.io/component=collector` should be
     the first thing anyone tries, and it should work. */}}

{{- define "observability.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "observability.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "observability.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "observability.labels" -}}
helm.sh/chart: {{ include "observability.chart" . }}
app.kubernetes.io/name: {{ include "observability.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: observability-stack
{{- with .Values.global.labels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
Selector labels for one component. Reads .component from the calling context:

    {{- include "observability.selectorLabels" (dict "ctx" . "component" "collector") | nindent 6 }}
*/}}
{{- define "observability.selectorLabels" -}}
app.kubernetes.io/name: {{ include "observability.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "observability.componentLabels" -}}
{{ include "observability.labels" .ctx }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/* Service DNS names other components use to reach each other. */}}
{{- define "observability.tempo.endpoint" -}}
{{ include "observability.fullname" . }}-tempo.{{ .Release.Namespace }}.svc.{{ .Values.clusterDomain | default "cluster.local" }}:4317
{{- end }}

{{- define "observability.prometheus.endpoint" -}}
http://{{ include "observability.fullname" . }}-prometheus.{{ .Release.Namespace }}.svc.{{ .Values.clusterDomain | default "cluster.local" }}:9090
{{- end }}

{{- define "observability.loki.endpoint" -}}
http://{{ include "observability.fullname" . }}-loki.{{ .Release.Namespace }}.svc.{{ .Values.clusterDomain | default "cluster.local" }}:3100
{{- end }}

{{/* Grafana admin credentials — existing secret wins, values are the fallback. */}}
{{- define "observability.grafana.secretName" -}}
{{- if .Values.grafana.admin.existingSecret }}
{{- .Values.grafana.admin.existingSecret }}
{{- else }}
{{- printf "%s-grafana-admin" (include "observability.fullname" .) }}
{{- end }}
{{- end }}

{{/* Secret holding contact-point credentials. */}}
{{- define "observability.alerting.secretName" -}}
{{- if .Values.grafana.alerting.existingSecret }}
{{- .Values.grafana.alerting.existingSecret }}
{{- else }}
{{- printf "%s-alerting" (include "observability.fullname" .) }}
{{- end }}
{{- end }}

{{- define "observability.storageClass" -}}
{{- with .storageClass }}storageClassName: {{ . }}{{ end }}
{{- end }}
