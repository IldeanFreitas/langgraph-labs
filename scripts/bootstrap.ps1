#Requires -Version 5.1
<#
.SYNOPSIS
    Prepara o ambiente de desenvolvimento dos labs LangGraph no VS Code.

.DESCRIPTION
    Idempotente: pode rodar quantas vezes quiser. Nao sobrescreve o .env nem os
    arquivos do .vscode que voce ja tenha editado (use -Force para isso).

    Etapas:
      1. pre-requisitos (uv, git, docker, code)
      2. Python 3.12 gerenciado pelo uv
      3. uv sync (cria .venv e instala as dependencias travadas)
      4. .env a partir do .env.example
      5. arquivos do VS Code (settings, extensions, tasks, launch)
      6. .editorconfig
      7. git init
      8. Postgres via docker compose
      9. gate de validacao da BrasilAPI
     10. abre o VS Code

.PARAMETER SkipDocker
    Nao sobe o Postgres. Ele so e necessario a partir do lab 3.

.PARAMETER SkipValidation
    Pula o gate da BrasilAPI (util quando voce esta sem rede).

.PARAMETER Force
    Sobrescreve os arquivos de configuracao do VS Code e o .editorconfig.

.PARAMETER NoOpen
    Nao abre o VS Code ao final.

.EXAMPLE
    .\scripts\bootstrap.ps1

.EXAMPLE
    .\scripts\bootstrap.ps1 -SkipDocker -NoOpen
#>
[CmdletBinding()]
param(
    [switch]$SkipDocker,
    [switch]$SkipValidation,
    [switch]$Force,
    [switch]$NoOpen
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:ExitCode = 0

$Root = Split-Path -Parent $PSScriptRoot
$script:Warnings = @()
$script:Step = 0

# ---------------------------------------------------------------- utilidades

function Write-Step([string]$Message) {
    $script:Step++
    Write-Host ""
    Write-Host ("[{0}/10] {1}" -f $script:Step, $Message) -ForegroundColor Cyan
}

function Write-Ok([string]$Message) {
    Write-Host "      OK  $Message" -ForegroundColor Green
}

function Write-Skip([string]$Message) {
    Write-Host "      --  $Message" -ForegroundColor DarkGray
}

function Write-Warn([string]$Message) {
    Write-Host "      !!  $Message" -ForegroundColor Yellow
    $script:Warnings += $Message
}

function Test-Command([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

# Comandos nativos (uv, git, docker, pytest) escrevem em stderr durante a operacao
# normal. Com $ErrorActionPreference = 'Stop', esse stderr vira erro terminante e
# derruba o script. Por isso toda chamada nativa passa por aqui, com a preferencia
# rebaixada para 'Continue' e o resultado julgado pelo codigo de saida.
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

function Get-NativeOutput {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )
    $anterior = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $saida = & $FilePath @Arguments 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return (($saida | Out-String).Trim())
    }
    finally { $ErrorActionPreference = $anterior }
}

function Update-PathFromRegistry {
    $machine = [System.Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [System.Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Write-ConfigFile([string]$Path, [string]$Content, [string]$Label) {
    if ((Test-Path -LiteralPath $Path) -and -not $Force) {
        Write-Skip "$Label ja existe (use -Force para sobrescrever)"
        return
    }
    Write-Utf8NoBom -Path $Path -Content $Content
    Write-Ok "$Label gravado"
}

# ------------------------------------------------------------------ conteudo

$VsCodeSettings = @'
{
  "python.defaultInterpreterPath": "${workspaceFolder}\\.venv\\Scripts\\python.exe",
  "python.testing.pytestEnabled": true,
  "python.testing.pytestArgs": ["tests"],
  "python.analysis.typeCheckingMode": "basic",
  "python.envFile": "${workspaceFolder}/.env",
  "[python]": {
    "editor.defaultFormatter": "charliermarsh.ruff",
    "editor.formatOnSave": true,
    "editor.codeActionsOnSave": { "source.organizeImports.ruff": "explicit" }
  },
  "[json]": { "editor.formatOnSave": true },
  "files.exclude": {
    "**/__pycache__": true,
    "**/.ruff_cache": true,
    "**/.pytest_cache": true,
    "**/.langgraph_api": true
  },
  "search.exclude": { "**/.venv": true },
  "rest-client.environmentVariables": {
    "$shared": { "brasilapi": "https://brasilapi.com.br/api" }
  }
}
'@

$VsCodeExtensions = @'
{
  "recommendations": [
    "ms-python.python",
    "ms-python.vscode-pylance",
    "charliermarsh.ruff",
    "ms-azuretools.vscode-containers",
    "humao.rest-client",
    "ms-toolsai.jupyter",
    "bierner.markdown-mermaid",
    "tamasfe.even-better-toml"
  ]
}
'@

$VsCodeTasks = @'
{
  "version": "2.0.0",
  "tasks": [
    {
      "label": "deps: uv sync",
      "type": "shell",
      "command": "uv sync",
      "problemMatcher": []
    },
    {
      "label": "qa: ruff",
      "type": "shell",
      "command": "uv run ruff check --fix . ; uv run ruff format .",
      "problemMatcher": []
    },
    {
      "label": "qa: pytest",
      "type": "shell",
      "command": "uv run pytest -v",
      "group": { "kind": "test", "isDefault": true },
      "problemMatcher": []
    },
    {
      "label": "gate: validar BrasilAPI",
      "type": "shell",
      "command": "uv run pytest tests/test_brasilapi.py -v",
      "problemMatcher": []
    },
    {
      "label": "infra: Postgres up",
      "type": "shell",
      "command": "docker compose up -d",
      "problemMatcher": []
    },
    {
      "label": "infra: Postgres down",
      "type": "shell",
      "command": "docker compose down",
      "problemMatcher": []
    },
    {
      "label": "run: langgraph dev",
      "type": "shell",
      "command": "uv run langgraph dev",
      "isBackground": true,
      "problemMatcher": []
    }
  ]
}
'@

$VsCodeLaunch = @'
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Python: arquivo atual",
      "type": "debugpy",
      "request": "launch",
      "program": "${file}",
      "console": "integratedTerminal",
      "justMyCode": false,
      "envFile": "${workspaceFolder}/.env"
    },
    {
      "name": "Pytest: arquivo atual",
      "type": "debugpy",
      "request": "launch",
      "module": "pytest",
      "args": ["${file}", "-v"],
      "console": "integratedTerminal",
      "justMyCode": false,
      "envFile": "${workspaceFolder}/.env"
    }
  ]
}
'@

$EditorConfig = @'
root = true

[*]
charset = utf-8
end_of_line = lf
insert_final_newline = true
trim_trailing_whitespace = true
indent_style = space
indent_size = 4

[*.{json,yml,yaml,md}]
indent_size = 2

[*.md]
trim_trailing_whitespace = false

[*.ps1]
end_of_line = crlf
'@

# -------------------------------------------------------------------- inicio

Write-Host ""
Write-Host "  Labs LangGraph - preparacao do ambiente" -ForegroundColor White
Write-Host "  $Root" -ForegroundColor DarkGray

Push-Location $Root
try {

    # 1 -------------------------------------------------------------------
    Write-Step "Pre-requisitos"

    if (-not (Test-Command 'uv')) {
        Write-Host "      instalando uv..." -ForegroundColor DarkGray
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        Update-PathFromRegistry
        if (-not (Test-Command 'uv')) {
            throw "uv foi instalado mas nao entrou no PATH. Feche e reabra o terminal e rode o script de novo."
        }
    }
    Write-Ok (Get-NativeOutput -FilePath 'uv' -Arguments '--version')

    if (Test-Command 'git') { Write-Ok "git presente" }
    else { Write-Warn "git nao encontrado - o passo 7 sera pulado" }

    if (Test-Command 'code') { Write-Ok "VS Code no PATH" }
    else { Write-Warn "comando 'code' nao encontrado - abra o projeto manualmente" }

    $dockerOk = $false
    if (Test-Command 'docker') {
        Invoke-Native -FilePath 'docker' -Arguments 'info' -Quiet
        if ($script:ExitCode -eq 0) {
            $dockerOk = $true
            Write-Ok "Docker respondendo"
        }
        else {
            Write-Warn "Docker instalado mas o daemon nao responde - abra o Docker Desktop e espere ficar verde. Necessario so a partir do lab 3."
        }
    }
    else { Write-Warn "Docker nao encontrado - necessario so a partir do lab 3" }

    # 2 -------------------------------------------------------------------
    Write-Step "Python 3.12 gerenciado pelo uv"
    Invoke-Native -FilePath 'uv' -Arguments @('python', 'install', '3.12')
    if ($script:ExitCode -ne 0) { throw "falha ao instalar o Python 3.12 pelo uv (codigo $script:ExitCode)" }
    Write-Ok "Python 3.12 disponivel"

    # 3 -------------------------------------------------------------------
    Write-Step "Dependencias (uv sync)"
    Invoke-Native -FilePath 'uv' -Arguments 'sync'
    if ($script:ExitCode -ne 0) { throw "uv sync falhou (codigo $script:ExitCode) - veja o erro acima" }

    $versao = Get-NativeOutput -FilePath 'uv' -Arguments @(
        'run', 'python', '-c', 'import langgraph; print(langgraph.__version__)'
    )
    if ($versao) { Write-Ok "langgraph $versao instalado em .venv" }
    else { Write-Warn "nao consegui ler a versao do langgraph" }

    # 4 -------------------------------------------------------------------
    Write-Step "Arquivo .env"
    $envPath = Join-Path $Root '.env'
    if (Test-Path -LiteralPath $envPath) {
        Write-Skip ".env ja existe - preservado"
    }
    else {
        Copy-Item (Join-Path $Root '.env.example') $envPath
        Write-Ok ".env criado a partir do .env.example"
        Write-Warn "preencha GOOGLE_API_KEY em .env - pegue em https://aistudio.google.com/apikey"
    }

    # 5 -------------------------------------------------------------------
    Write-Step "Configuracao do VS Code"
    $vs = Join-Path $Root '.vscode'
    Write-ConfigFile (Join-Path $vs 'settings.json')   $VsCodeSettings   '.vscode/settings.json'
    Write-ConfigFile (Join-Path $vs 'extensions.json') $VsCodeExtensions '.vscode/extensions.json'
    Write-ConfigFile (Join-Path $vs 'tasks.json')      $VsCodeTasks      '.vscode/tasks.json'
    Write-ConfigFile (Join-Path $vs 'launch.json')     $VsCodeLaunch     '.vscode/launch.json'

    # 6 -------------------------------------------------------------------
    Write-Step "EditorConfig"
    Write-ConfigFile (Join-Path $Root '.editorconfig') $EditorConfig '.editorconfig'

    # 7 -------------------------------------------------------------------
    Write-Step "Repositorio git"
    if (-not (Test-Command 'git')) {
        Write-Skip "git ausente"
    }
    elseif (Test-Path -LiteralPath (Join-Path $Root '.git')) {
        Write-Skip "repositorio ja inicializado"
    }
    else {
        Invoke-Native -FilePath 'git' -Arguments @('init', '--initial-branch=main') -Quiet
        if ($script:ExitCode -ne 0) {
            # git anterior a 2.28 nao conhece --initial-branch
            Invoke-Native -FilePath 'git' -Arguments 'init' -Quiet
        }
        if ($script:ExitCode -eq 0) { Write-Ok "repositorio criado (nada commitado - o primeiro commit e seu)" }
        else { Write-Warn "git init falhou (codigo $script:ExitCode)" }
    }

    # 8 -------------------------------------------------------------------
    Write-Step "Postgres (checkpointer do lab 3 em diante)"
    if ($SkipDocker) {
        Write-Skip "pulado por -SkipDocker"
    }
    elseif (-not $dockerOk) {
        Write-Skip "Docker indisponivel - rode 'docker compose up -d' quando chegar no lab 3"
    }
    else {
        Invoke-Native -FilePath 'docker' -Arguments @('compose', 'up', '-d')
        if ($script:ExitCode -eq 0) { Write-Ok "container langgraph-labs-pg no ar (localhost:5432)" }
        else { Write-Warn "docker compose up falhou (codigo $script:ExitCode) - veja o erro acima" }
    }

    # 9 -------------------------------------------------------------------
    Write-Step "Gate: a BrasilAPI responde?"
    if ($SkipValidation) {
        Write-Skip "pulado por -SkipValidation"
    }
    else {
        Invoke-Native -FilePath 'uv' -Arguments @('run', 'pytest', 'tests/test_brasilapi.py', '-q')
        if ($script:ExitCode -eq 0) {
            Write-Ok "BrasilAPI validada - a fundacao esta de pe"
        }
        else {
            Write-Warn "o gate falhou. O problema esta em rede ou na API, nao no LangGraph. Nao siga para o lab 1 antes de resolver."
        }
    }

    # 10 ------------------------------------------------------------------
    Write-Step "Abrir o VS Code"
    if ($NoOpen) {
        Write-Skip "pulado por -NoOpen"
    }
    elseif (Test-Command 'code') {
        code $Root
        Write-Ok "VS Code aberto - aceite a sugestao de instalar as extensoes recomendadas"
    }
    else {
        Write-Skip "abra manualmente: $Root"
    }

    # resumo --------------------------------------------------------------
    Write-Host ""
    if ($script:Warnings.Count -eq 0) {
        Write-Host "  Ambiente pronto." -ForegroundColor Green
    }
    else {
        Write-Host "  Ambiente preparado, com pendencias:" -ForegroundColor Yellow
        foreach ($w in $script:Warnings) { Write-Host "    - $w" -ForegroundColor Yellow }
    }
    Write-Host ""
    Write-Host "  Proximo passo: preencher GOOGLE_API_KEY no .env e rodar o lab 1." -ForegroundColor White
    Write-Host "  Tarefas do VS Code: Ctrl+Shift+P > 'Tasks: Run Task'" -ForegroundColor DarkGray
    Write-Host ""
}
catch {
    Write-Host ""
    Write-Host "  FALHOU: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ""
    exit 1
}
finally {
    Pop-Location
}
