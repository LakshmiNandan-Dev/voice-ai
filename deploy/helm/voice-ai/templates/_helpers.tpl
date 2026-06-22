{{- define "voice-ai.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "voice-ai.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "voice-ai.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "voice-ai.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: voice-ai
{{- end -}}

{{/* Build a fully-qualified image reference. Registry optional (local images). */}}
{{- define "voice-ai.image" -}}
{{- $registry := .root.Values.image.registry -}}
{{- if $registry -}}
{{ $registry }}/{{ .repo }}:{{ .root.Values.image.tag }}
{{- else -}}
{{ .repo }}:{{ .root.Values.image.tag }}
{{- end -}}
{{- end -}}

{{/* Resolved Redis URL: in-cluster service or external. */}}
{{- define "voice-ai.redisUrl" -}}
{{- if .Values.redis.deploy -}}
redis://{{ include "voice-ai.fullname" . }}-redis:6379/0
{{- else -}}
{{ .Values.redis.url }}
{{- end -}}
{{- end -}}

{{/* Resolved Postgres URL: in-cluster service or external. */}}
{{- define "voice-ai.databaseUrl" -}}
{{- if .Values.postgres.deploy -}}
postgresql://{{ .Values.postgres.user }}:{{ .Values.postgres.password }}@{{ include "voice-ai.fullname" . }}-postgres:5432/{{ .Values.postgres.database }}
{{- else -}}
{{ .Values.postgres.url }}
{{- end -}}
{{- end -}}
