#Requires -Version 5.1
<#
.SYNOPSIS
    Inventaria e limpa o Docker antes de rodar os labs LangGraph.

.DESCRIPTION
    Por padrao roda em modo INVENTARIO: nao apaga nada, so mostra o que existe
    e quanto espaco esta em uso. Use -Mode para escolher o nivel de limpeza.

    Modos:
      Inventario (padrao) - so lista. Nenhuma alteracao.
      Seguro              - remove containers parados, redes orfas, imagens
                            pendentes (dangling) e cache de build. Preserva
                            imagens em uso, volumes nomeados e dados.
      Total               - remove TODAS as imagens nao usadas por container
                            ativo e TODOS os volumes nao usados, inclusive os
                            nomeados. DESTRUTIVO E IRREVERSIVEL.

.PARAMETER Mode
    Inventario | Seguro | Total

.PARAMETER Yes
    Nao pergunta. So tem efeito no modo Total, e ainda assim exige que voce
    tenha visto o inventario antes.

.EXAMPLE
    .\scripts\docker-reset.ps1
    Mostra o inventario. Comece sempre por aqui.

.EXAMPLE
    .\scripts\docker-reset.ps1 -Mode Seguro

.EXAMPLE
    .\scripts\docker-reset.ps1 -Mode Total
#>
[CmdletBinding()]
param(
    [ValidateSet('Inventario', 'Seguro', 'Total')]
    [string]$Mode = 'Inventario',
    [switch]$Yes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:ExitCode = 0

function Invoke-Native {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [switch]$Quiet
    )
    # Nao retorna valor: a saida do comando flui direto para o console.
    # O codigo de saida fica em $script:ExitCode. Retornar o codigo pelo
    # pipeline misturaria stdout com o codigo e engoliria a saida do comando.
    $anterior = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($Quiet) { & $FilePath @Arguments *> $null }
        else        { & $FilePath @Arguments }
        $script:ExitCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $anterior }
}

function Write-Title([string]$T) {
    Write-Host ""
    Write-Host "  $T" -ForegroundColor Cyan
    Write-Host ("  " + ("-" * $T.Length)) -ForegroundColor DarkGray
}

# ---------------------------------------------------------------- verificacao

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "Docker nao encontrado no PATH." -ForegroundColor Red
    exit 1
}
if ((Invoke-Native -FilePath 'docker' -Arguments 'info' -Quiet) -ne 0) {
    Write-Host "O daemon do Docker nao responde. Abra o Docker Desktop e espere ficar verde." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- inventario

Write-Title "Espaco em uso"
Invoke-Native -FilePath 'docker' -Arguments @('system', 'df')

Write-Title "Containers (todos)"
Invoke-Native -FilePath 'docker' -Arguments @(
    'ps', '-a', '--format', 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
) | Out-Null

Write-Title "Imagens"
Invoke-Native -FilePath 'docker' -Arguments @(
    'images', '--format', 'table {{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}'
) | Out-Null

Write-Title "Volumes"
Invoke-Native -FilePath 'docker' -Arguments @(
    'volume', 'ls', '--format', 'table {{.Name}}\t{{.Driver}}'
) | Out-Null

if ($Mode -eq 'Inventario') {
    Write-Host ""
    Write-Host "  Modo inventario: nada foi alterado." -ForegroundColor Green
    Write-Host "  Revise a lista acima. Se reconhecer imagens de outros projetos" -ForegroundColor DarkGray
    Write-Host "  (Aurora, Airbyte/kind, Oracle, SQL Server), pense duas vezes" -ForegroundColor DarkGray
    Write-Host "  antes do modo Total - sao muitos GB para baixar de novo." -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  Proximo passo:  .\scripts\docker-reset.ps1 -Mode Seguro" -ForegroundColor White
    Write-Host ""
    exit 0
}

# ------------------------------------------------------------------- limpeza

if ($Mode -eq 'Seguro') {
    Write-Title "Limpeza segura"
    Write-Host "  Remove: containers parados, redes orfas, imagens dangling, cache de build." -ForegroundColor DarkGray
    Write-Host "  Preserva: imagens em uso, imagens tagueadas sem container, TODOS os volumes." -ForegroundColor DarkGray
    Write-Host ""
    Invoke-Native -FilePath 'docker' -Arguments @('system', 'prune', '-f')
    if ($script:ExitCode -ne 0) { Write-Host "  falhou (codigo $script:ExitCode)" -ForegroundColor Red; exit 1 }
}
elseif ($Mode -eq 'Total') {
    Write-Title "LIMPEZA TOTAL - DESTRUTIVA"
    Write-Host "  Vai remover:" -ForegroundColor Yellow
    Write-Host "    - todas as imagens sem container ativo (inclusive tagueadas)" -ForegroundColor Yellow
    Write-Host "    - todos os volumes nao usados, INCLUSIVE os nomeados" -ForegroundColor Yellow
    Write-Host "    - containers parados, redes orfas e cache de build" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Isso apaga os dados de qualquer laboratorio que esteja parado." -ForegroundColor Yellow
    Write-Host "  Nao ha desfazer. As imagens terao de ser baixadas de novo." -ForegroundColor Yellow
    Write-Host ""

    if (-not $Yes) {
        $resposta = Read-Host "  Digite APAGAR TUDO para confirmar"
        if ($resposta -ne 'APAGAR TUDO') {
            Write-Host "  Cancelado. Nada foi alterado." -ForegroundColor Green
            exit 0
        }
    }

    Write-Host ""
    Write-Host "  [1/3] containers, redes, imagens e cache..." -ForegroundColor DarkGray
    Invoke-Native -FilePath 'docker' -Arguments @('system', 'prune', '-a', '-f')

    Write-Host "  [2/3] volumes (inclusive nomeados)..." -ForegroundColor DarkGray
    Invoke-Native -FilePath 'docker' -Arguments @('volume', 'prune', '-a', '-f')

    Write-Host "  [3/3] cache de build restante..." -ForegroundColor DarkGray
    Invoke-Native -FilePath 'docker' -Arguments @('builder', 'prune', '-a', '-f')
}

# -------------------------------------------------------------------- estado

Write-Title "Espaco apos a limpeza"
Invoke-Native -FilePath 'docker' -Arguments @('system', 'df')

Write-Title "Subindo o Postgres dos labs"
$compose = Join-Path (Split-Path -Parent $PSScriptRoot) 'docker-compose.yml'
if (Test-Path -LiteralPath $compose) {
    Push-Location (Split-Path -Parent $PSScriptRoot)
    try {
        Invoke-Native -FilePath 'docker' -Arguments @('compose', 'up', '-d')
        if ($script:ExitCode -eq 0) {
            Write-Host ""
            Write-Host "  Postgres dos labs no ar em localhost:5432" -ForegroundColor Green
        }
        else { Write-Host "  docker compose up falhou (codigo $script:ExitCode)" -ForegroundColor Red }
    }
    finally { Pop-Location }
}
else { Write-Host "  docker-compose.yml nao encontrado" -ForegroundColor Yellow }

Write-Host ""
