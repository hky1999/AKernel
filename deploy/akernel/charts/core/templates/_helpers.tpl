{{/*
Expand the name of the chart.
*/}}
{{- define "core.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Shared filesystem configuration is identical for capture and reclamation. */}}
{{- define "core.checkpointStorage" -}}
{{- $storage := .Values.adx.checkpointStorage -}}
{{- dict "kind" "distributed_fs" "alias" $storage.alias "mount_root" $storage.mountPath "prefix" $storage.prefix | toYaml -}}
{{- end -}}

{{- define "core.checkpointVolume" -}}
name: checkpoint-storage
{{- if .Values.adx.checkpointStorage.existingClaim }}
persistentVolumeClaim:
  claimName: {{ .Values.adx.checkpointStorage.existingClaim | quote }}
{{- else }}
hostPath:
  path: {{ .Values.adx.checkpointStorage.hostPath | quote }}
  type: Directory
{{- end }}
{{- end -}}

{{- define "core.checkpointMount" -}}
name: checkpoint-storage
mountPath: {{ .Values.adx.checkpointStorage.mountPath | quote }}
{{- if .Values.adx.checkpointStorage.hostPath }}
mountPropagation: HostToContainer
{{- end }}
{{- end -}}


{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "core.fullname" -}}
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

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "core.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "core.labels" -}}
helm.sh/chart: {{ include "core.chart" . }}
{{ include "core.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "core.selectorLabels" -}}
app.kubernetes.io/name: {{ include "core.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "core.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "core.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Resolve component image settings. control/node default to the global
all-in-one image, while each component can still override repository/tag/policy.
*/}}
{{- define "core.image.repository" -}}
{{- $root := .root -}}
{{- $image := .image | default dict -}}
{{- default $root.Values.image.repository $image.repository -}}
{{- end }}

{{- define "core.image.tag" -}}
{{- $root := .root -}}
{{- $image := .image | default dict -}}
{{- default $root.Values.image.tag $image.tag -}}
{{- end }}

{{- define "core.image.pullPolicy" -}}
{{- $root := .root -}}
{{- $image := .image | default dict -}}
{{- default $root.Values.image.pullPolicy $image.pullPolicy -}}
{{- end }}

{{- define "core.image" -}}
{{- printf "%s:%s" (include "core.image.repository" .) (include "core.image.tag" .) -}}
{{- end }}
