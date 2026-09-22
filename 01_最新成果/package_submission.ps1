param(
    [Parameter(Mandatory = $true)]
    [string]$TeamName,

    [Parameter(Mandatory = $true)]
    [string]$ParticipantForm,

    [string]$FinalPptx = "初赛方案-RefSync-QA-v2.4.pptx",

    [string]$DemoVideo = "RefSync-QA-数据测试演示.mp4"
)

$submissionDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pptxPath = Join-Path $submissionDir $FinalPptx
$formPath = (Resolve-Path -LiteralPath $ParticipantForm -ErrorAction Stop).Path
$workbookPath = Join-Path $submissionDir "RefSync-QA-全量检测与验证报告.xlsx"
$answerPath = Join-Path $submissionDir "初赛答题方案.md"
$environmentPath = Join-Path $submissionDir "复现环境.md"
$attachmentPath = Join-Path $submissionDir "附件"
$videoPath = Join-Path $submissionDir $DemoVideo
$videoExists = Test-Path -LiteralPath $videoPath -PathType Leaf

if (-not (Test-Path -LiteralPath $pptxPath -PathType Leaf)) { throw "PPT 不存在：$pptxPath" }
if (-not (Test-Path -LiteralPath $workbookPath -PathType Leaf)) { throw "全量报告不存在：$workbookPath" }
if (-not (Test-Path -LiteralPath $attachmentPath -PathType Container)) { throw "附件目录不存在：$attachmentPath" }
if ($TeamName -match "待补|填写|团队名称") { throw "TeamName 仍是占位文本，请填写真实团队名称。" }

$safeTeamName = $TeamName -replace '[\\/:*?"<>|]', '_'
$zipName = "多模态数据质量检测算法竞赛-$safeTeamName-RefSync-QA.zip"
$zipPath = Join-Path $submissionDir $zipName
if (Test-Path -LiteralPath $zipPath) { throw "目标 ZIP 已存在，请先改名或移走：$zipPath" }

$items = @($pptxPath, $formPath, $workbookPath, $answerPath, $environmentPath, $attachmentPath)
if ($videoExists) { $items += $videoPath } else { Write-Host "提示：未找到演示视频 $DemoVideo，按可选项跳过。" }
Compress-Archive -LiteralPath $items -DestinationPath $zipPath -CompressionLevel Optimal

$sizeMb = [math]::Round((Get-Item -LiteralPath $zipPath).Length / 1MB, 2)
if ($sizeMb -ge 200) { throw "ZIP 为 $sizeMb MB，超过 200MB 限制；请移出非必要附件后重试。" }
Write-Host "已生成：$zipPath"
Write-Host "大小：$sizeMb MB"
Write-Host "请打开 ZIP 复查 PPT、参赛表和团队名后再上传。"
