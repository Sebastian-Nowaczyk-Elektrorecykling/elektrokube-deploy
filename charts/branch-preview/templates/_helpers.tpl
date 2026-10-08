{{- define "preview.url" -}}
{{- trimSuffix ".git" (trimSuffix "/" (required "repository.url is required" .Values.repository.url)) -}}
{{- end -}}
{{- define "preview.repo" -}}
{{- trimPrefix "https://github.com/" (include "preview.url" .) -}}
{{- end -}}
{{- define "preview.image" -}}
{{- default (printf "ghcr.io/%s" (lower (include "preview.repo" .))) .Values.registry.image -}}
{{- end -}}
{{- define "preview.name" -}}
{{- printf "%s-%s" (.Release.Name | trunc 13 | trimSuffix "-") (.Release.Name | sha256sum | trunc 6) -}}
{{- end -}}
{{- define "preview.buildID" -}}
{{- printf "%s\n%s\n%s" (toJson .Values) (.Files.Get "files/runner.py") .Chart.Version | sha256sum | trunc 10 -}}
{{- end -}}
{{- define "preview.repoLabel" -}}
{{- $repo := last (splitList "/" (include "preview.repo" .)) -}}
{{- $label := regexReplaceAll "[^a-z0-9-]+" (lower $repo) "-" | trimAll "-" -}}
{{- if or (ne (lower $repo) $label) (gt (len $label) 63) -}}
{{- $label = printf "%s--%s" ($label | trunc 45 | trimSuffix "-" | default "repo") ($repo | sha256sum | trunc 12) -}}
{{- end -}}
{{- default $label .Values.routing.repositoryLabel -}}
{{- end -}}
