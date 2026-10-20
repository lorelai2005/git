# export_pdf.ps1 — DOCX -> PDF（Microsoft Word COM，高保真，字体/表格与参考样例一致）
# 用法: pwsh -NoProfile -File export_pdf.ps1 -InFile a.docx -OutFile a.pdf
param(
    [Parameter(Mandatory = $true)][string]$InFile,
    [Parameter(Mandatory = $true)][string]$OutFile
)

$ErrorActionPreference = 'Stop'

$inPath = (Resolve-Path -LiteralPath $InFile).Path
$outFull = [System.IO.Path]::GetFullPath($OutFile)
$outDir = [System.IO.Path]::GetDirectoryName($outFull)
if (-not (Test-Path -LiteralPath $outDir)) {
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null
}
if (Test-Path -LiteralPath $outFull) { Remove-Item -LiteralPath $outFull -Force }

$word = $null
$doc = $null
try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    # 关闭可能弹窗的选项
    try { $word.Options.SaveInterval = 0 } catch {}
    try { $word.AutomationSecurity = 3 } catch {}

    $doc = $word.Documents.Open($inPath, $false, $true)   # ConfirmConversions=false, ReadOnly=true
    # 17 = wdExportFormatPDF, 0 = wdExportOptimizeForPrint
    $doc.ExportAsFixedFormat($outFull, 17, $false, 0, 0, 0, 0, 0)
    $doc.Close(0)
    $doc = $null
}
finally {
    if ($doc -ne $null) { try { $doc.Close(0) } catch {} }
    if ($word -ne $null) {
        try { $word.Quit() } catch {}
        try { [System.Runtime.InteropServices.Marshal]::ReleaseComObject($word) | Out-Null } catch {}
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}

if (-not (Test-Path -LiteralPath $outFull)) {
    Write-Error "PDF 未生成: $outFull"
    exit 1
}
$size = (Get-Item -LiteralPath $outFull).Length
Write-Output "PDF=$outFull BYTES=$size"
exit 0
