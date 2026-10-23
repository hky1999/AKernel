{{- define "juicefs.name" -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- end -}}

{{- define "juicefs.auth" -}}
{{- default (printf "%s-postgres" (include "juicefs.name" .)) .Values.postgres.existingSecret -}}
{{- end -}}

{{- define "juicefs.placement" -}}
{{- if . }}
nodeAffinity:
  requiredDuringSchedulingIgnoredDuringExecution:
    nodeSelectorTerms:
      - matchExpressions:
          {{- range $key, $value := . }}
          - key: {{ $key | quote }}
            operator: In
            values: [{{ $value | quote }}]
          {{- end }}
{{- else }}
{}
{{- end }}
{{- end -}}

{{- define "juicefs.env" -}}
- name: META_URL
  value: {{ printf "postgres://juicefs@%s-postgres.%s.svc.cluster.local:5432/juicefs?sslmode=disable" (include "juicefs.name" .) .Release.Namespace | quote }}
- name: META_PASSWORD
  valueFrom:
    secretKeyRef: {name: {{ include "juicefs.auth" . | quote }}, key: password}
- name: ACCESS_KEY
  valueFrom:
    secretKeyRef: {name: {{ printf "%s-bucket" (include "juicefs.name" .) | quote }}, key: AWS_ACCESS_KEY_ID}
- name: SECRET_KEY
  valueFrom:
    secretKeyRef: {name: {{ printf "%s-bucket" (include "juicefs.name" .) | quote }}, key: AWS_SECRET_ACCESS_KEY}
{{- end -}}
