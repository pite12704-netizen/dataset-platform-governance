# 本地目录 / 客户侧 MySQL + MinIO 接入工具

大量本地图片无需网页全选，也不用先放入客户 MinIO。安装下述依赖后，在包含 clients/ 的目录执行：

```powershell
python -m clients.ingest prepare-local `
  --input-dir 'D:\Photos' `
  --output 'D:\ImportManifests\run-001' `
  --source-id 'local-photos-001' `
  --source '填写真实来源' `
  --license '填写实际获准的使用许可'
```

它递归扫描 JPG/JPEG/PNG/WebP，冻结图片名单并生成 manifest.jsonl、summary.json、errors.json、skipped.jsonl，不额外复制原图。原图目录与输出目录不能互相包含；每张 1 字节～20MiB，单次最多 100000 张。非支持格式记录在 skipped.jsonl，数量显示在 summary.skipped_files；零字节、过大、消失或变化的图片使 complete=false，不能作为完整批次上传。新加入的图片不自动混入冻结清单；改过原图或来源配置应使用新输出目录。重跑准备命令会重新校验全部冻结图片，上传断点由服务器保存。

在草稿版本“批量导入”用 summary.json 创建批次，complete 必须为 true；再执行：

```powershell
python -m clients.ingest upload `
  --manifest 'D:\ImportManifests\run-001\manifest.jsonl' `
  --input-dir 'D:\Photos' `
  --platform 'https://你的平台域名' `
  --batch '网页显示的批次ID'
```

在隐藏提示中输入批次凭据。企业 CA 可加 `--ca-file <CA路径>`。上传时原图必须仍可读取，工具再次核对文件哈希与已收分片；中断后运行相同命令续传。同名文件用相对路径区分，链接、目录联接和越界路径拒绝读取。工具不根据文件夹自动认定同一人物或来源组；治理分组需要真实业务信息。目录清单和 SQLite 本地锁含业务文件信息，请保存到受保护且不入 Git 的目录。此 SQLite 不是平台数据库。

单批能登记 100000 文件是接口边界，治理、识别、空间配额和交付仍有各自限制，应先用小批真实数据验证；这里不承诺 60000 张整版治理或吞吐已验收。

新版服务记录分片 SHA-256，续传只跳过大小与 SHA 均匹配的片。旧服务没有分片 SHA 时保守重传当前未完成文件的相关分片，完整文件仍跳过；不把 S3 ETag 当作通用内容哈希。

需要 Python 3.11+。解压到客户能访问源 MySQL 和 MinIO 的机器，从包含 clients/ 的目录执行：

```text
python -m pip install -r clients/ingest/requirements.txt
python -m clients.ingest export --config <本机配置.json> --output <本机导出目录>
python -m clients.ingest upload --manifest <本机导出目录>/manifest.jsonl --platform https://<平台地址> --batch <平台批次ID>
```

先复制 source.example.json 并配置表/字段映射。MySQL 使用只读用户；MinIO 只需指定桶/前缀的 Head/Get 权限。把示例中 url 列映射到实际图片 URL 列，或改成 key 列映射；url_prefix 必须准确匹配该系统 URL 到对象键的前缀。支持可选 revision、group、annotation 和 version 列，id 必须为单列主键。URL 签名参数不会进入导出清单。version 列可绑定源 MinIO 对象版本；没有对象版本时采用条件读取检测传输期间变化，不能证明跨系统同一时刻快照。

凭据不写进 JSON。设置 CUSTOMER_DATABASE_URL、CUSTOMER_S3_ENDPOINT、CUSTOMER_S3_ACCESS_KEY、CUSTOMER_S3_SECRET_KEY 等示例中指定的环境变量；也可设置同名加 _FILE 后缀的环境变量，值为本机密钥文件路径。不要把密码放在命令行或提交源码。企业 CA 可在 storage.ca_file 配置，内网 HTTP 需要显式 allow_http；目标平台除回环测试外必须 HTTPS，上传加 --ca-file 指定企业 CA，不关闭证书校验。

export 输出 records.jsonl、图片缓存、manifest.jsonl、summary.json 和 errors.json。先修复所有导出失败，重复 export 使用同一快照及已校验缓存；源配置或快照内容变化请用新目录。导出目录包含业务数据，应按客户的数据和磁盘保护制度保管，不上传到 Git。

在平台草稿版本的“批量导入”页选择 summary.json 创建批次。复制仅展示一次的批次凭据，运行 upload，在隐藏提示中粘贴；凭据有效期 24 小时，过期可在平台轮换，然后运行同一命令续传。工具也可从 DP_IMPORT_TOKEN 环境变量读取凭据，但不在磁盘保存它。断网或主动中断后重新执行会向服务器核对分片进度。

检查任务暂时失败时，可在页面逐项重试，或对同一 upload 命令加 --retry-scans。内容校验不合格不会因重试变成通过，需修正来源并导出新的清单。上传默认等待检查最多 600 秒，--wait-seconds 可调整；超时不丢失平台进度。导入完成后仍须治理、人工复核、测评和发布。

退出码：0 完整成功；2 有失败或后台检查未结束；130 操作者中断。错误不输出源地址、密码或签名 URL，逐文件问题按来源主键在 errors.json 或平台导入明细定位。
