# 直播人工优选（2026-11-16）

不是新发现的万能源。**目前只相对推荐2个小列表、每表1台**，均取自原聚合已有线路，扩搜后按持续证据重新筛选/排序；不把镜像数量当底层多样性。央视/湖南/少儿尚无新合格替代，原11订阅保留覆盖但取消整表流畅推荐。

| 列表 | 唯一频道/完整媒体URL | 本次结果 |
| --- | --- | --- |
| international.m3u | CGTN英语 `http://english-livetx.cgtn.com/hls/yypdyyctzb_hd.m3u8` | 两轮180秒供给比1.056/1.111；600秒61片/610媒体秒，供给比1.0167、P95下载/媒体0.0487、0失败/漏片/模拟不足；720p25fps。60秒视频解码1500帧dup/drop0，存在HTTP EOF警告，不能说完全无警告。 |
| phoenix.m3u | 凤凰资讯 `http://43.152.224.209/qctv.fengshows.cn/live/0701pin72.flv` | 两轮180秒各4500帧无警告；600秒601.759墙钟秒、15000帧/600媒体秒、dup/drop0，媒体/墙钟0.9971；有最大8.092秒读入追赶警告，严格无警告门槛未过，仅相对备选。720p25fps视频约999kbps、AAC约47kbps。 |

这两台底层不同，**不代表央卫或全港澳台替代，也不保证手机/晚高峰/异地**。首分片首字节不是首帧；HLS供给不足是模型，不是播放器真实卡顿率；FLV是节流视频解码，不与HLS供给指标混算。未检验实际节目/画面真实性、音画同步。不能说已证实用户网络或播放器是根因。

## 明确不入选

- 原zbds/91的CCTV13 `http://ali-m-l.cztv.com/channels/lantian/channel21/1080p.m3u8`、浙江少儿 `http://ali-vl.cztv.com/channels/lantian/channel008/360p.m3u8`，以及Daily A浙江 `http://ali-m-l.cztv.com/channels/lantian/channel001/1080p.m3u8`：180秒仅16秒独特媒体，供给约0.089，playlist冻结。
- 原湖南 `https://txmov2.a.kwimgs.com/bs3/video-hls/5199687338554291078_hlsb.m3u8`、央卫CCTV1 `https://p2.bdstatic.com/rtmp.liveshow.lss-user.baidubce.com/live/stream_bduid_6488563992_8982723413/merged_1707497046297_88536_25144_41936.m3u8`：ENDLIST录播/占位，绝不改用解码成功洗成直播。
- 新分层6条央视/卫视180秒供给比分别约0.518(CCTV1)、0.389(CCTV13)、0.574(湖南)、0(浙江)、0.657(河北)、0.028(山东)，不因基础短片通过硬收。
- 广西官网线路 `https://hlscdn.liangtv.cn/live/0c4ef3a44b934cacb8b47121dfada66c/d7e04258157b480dae53883cc6f8123b.m3u8`：两轮180秒供给良好，但600秒有4.053秒模拟不足/2个慢片；再用ffprobe及60秒解码发现大量AAC/H264损坏/损坏画面，**不入选**。ffmpeg退出0也不能据此宣称流畅。此URL本就存在测速聚合，并非新CDN。

## 来源与维护

`selected.json`保存每条公开电视台、完整URL、请求头、上游出处、实测覆盖；`evidence-2026-11-16.json`保存供给与解码总结及真实警告。完整逐请求日志仅在维护Mac的.audit-work保留，不包含进公共提交。小白名单**不自动拉取整张第三方列表**，避免上游自动混入成人/录播；频道内容未来仍需人工复核。

```bash
python3 scripts/refresh_curated_live.py --check
# 修改selected.json后渲染；会加.curated-live.lock，不抢占别人的owner
python3 scripts/refresh_curated_live.py
# 基础完整检测（远程发布后）：
python3 scripts/check_sources.py --live --full --phone --workers 4 --json-out /tmp/live-basic.json
# 然后单独HLS样本，两轮3分钟和优胜10分钟，不与基础/FLV另开并发：
python3 scripts/check_live_stability.py --source '国际新闻实测（CGTN）' --channel '^CGTN英语$' --seconds 180 --rounds 2 --json-out /tmp/cgtn-180.json
python3 scripts/check_live_stability.py --source '国际新闻实测（CGTN）' --channel '^CGTN英语$' --seconds 600 --json-out /tmp/cgtn-600.json
python3 scripts/check_continuous_stream.py --name 凤凰资讯 --url 'http://43.152.224.209/qctv.fengshows.cn/live/0701pin72.flv' --seconds 180 --rounds 2 --json-out /tmp/phoenix-180.json
python3 scripts/check_continuous_stream.py --name 凤凰资讯 --url 'http://43.152.224.209/qctv.fengshows.cn/live/0701pin72.flv' --seconds 600 --rounds 1 --json-out /tmp/phoenix-600.json
```

任何新卫视优胜者应补连续解码/错误审阅，不只看TS同步字节或ffmpeg退出码。先检查维护锁及PID，使用README的低并发顺序；无IPv6出口只跳过裸IPv6，不删源。严格样本指标不自动授予整聚合推荐；本次依用户要求保留2个有明示局限的相对推荐。周更调度另由维护者调整，本次不改routine。
