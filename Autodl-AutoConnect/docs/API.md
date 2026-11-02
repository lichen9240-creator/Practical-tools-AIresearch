# AutoDL 接口清单（本项目实际使用的）

本文件记录 `autodl_autoconnect/api.py` 里调用的每一个端点、请求体与返回结构，
方便你复核，也方便官方改版时对照修改。

## 鉴权

所有端点同一个凭据：

```
Authorization: <AutoDL 开发者 Token>
```

Token 位置：AutoDL 控制台 → 设置 → 开发者 Token。

响应统一是信封格式：

```json
{ "code": "Success", "msg": "", "data": { ... }, "request_id": "..." }
```

`code != "Success"` 一律视为业务失败。其中 `AuthorizeFailed` 等鉴权类错误码
会被翻译成「鉴权失败」，退出码 3。

> 官方文档只公开了 `POST /api/v1/dev/wallet/balance`、
> `POST /api/v1/dev/exclusive_nfs/mount` 等少数几个端点。
> 其余端点是 AutoDL 控制台网页自身在用的接口，**未公开文档**，
> 官方改版可能调整路径或字段。

## 官方文档端点

### 查询账户余额

```
POST /api/v1/dev/wallet/balance
```

请求参数：无。

返回：

```json
{
  "code": "Success",
  "data": {
    "assets": 1000,
    "accumulate": 1000,
    "voucher_balance": 1000
  },
  "msg": ""
}
```

金额单位是**千分之一元**（`assets / 1000` = 元）。

### 按地区查 GPU 库存

```
POST /api/v1/dev/machine/region/gpu_stock
```

请求体：

```json
{
  "region_sign": "westDC2",
  "cuda_v_from": 117,
  "cuda_v_to": 128
}
```

- `region_sign`：地区代码，见下方地区表
- `cuda_v_from` / `cuda_v_to`：CUDA 版本范围（可选），117 表示 11.7

返回：

```json
{
  "code": "Success",
  "msg": "",
  "data": [
    { "RTX 4090":  { "idle_gpu_num": 215, "total_gpu_num": 2285 } },
    { "RTX 3080 Ti": { "idle_gpu_num": 20, "total_gpu_num": 392 } }
  ]
}
```

## 控制台端点

### 搜索机器报价（抢卡主用）

```
POST /api/v1/machine/search
```

请求体（默认值取自 AutoDL 控制台自己发出的请求）：

```json
{
  "charge_type": "payg",
  "region_sign": "",
  "gpu_type_name": [],
  "machine_tag_name": [],
  "gpu_idle_num": 0,
  "mount_net_disk": false,
  "instance_disk_size_order": "",
  "date_range": "",
  "date_from": "",
  "date_to": "",
  "page_index": 1,
  "page_size": 90,
  "pay_price_order": "",
  "gpu_idle_type": "",
  "default_order": true,
  "region_sign_list": []
}
```

本项目只把 `region_sign_list`（地区代码，语义精确）下推给服务端，
**型号、卡数、价格一律在本地过滤**——服务端对型号是精确匹配，
用户写 `4090` 会被直接过滤成空结果。

返回：

```json
{
  "code": "Success",
  "data": {
    "list": [
      {
        "machine_id": "b7f3c9a2",
        "region_sign": "westDC2",
        "gpu_type_name": ["RTX 4090"],
        "gpu_idle_num": 4,
        "gpu_total_num": 10,
        "max_instance_num": 8,
        "binding_instance_num": 0,
        "on_line": 1,
        "pay_price": 2.5
      }
    ],
    "result_total": 1
  }
}
```

等价端点 `POST /api/v1/user/machine/list` 返回同样结构，
客户端在结构对不上时会自动再试它一次。

`on_line` 取值 3/4/5/6 表示机器不在线，客户端会跳过。

### 创建按量计费实例（真正下单）

```
POST /api/v1/order/instance/create/payg
```

请求体：

```json
{
  "instance_info": {
    "machine_id": "b7f3c9a2",
    "charge_type": "payg",
    "req_gpu_amount": 1,
    "base_image_uuid": "<官方镜像 uuid>",
    "private_image_uuid": "",
    "reproduction_uuid": "",
    "cg_application_uuid": "",
    "cg_application_info": {
      "image_name": "",
      "current_version_id": 0,
      "current_version": "",
      "image_id": 0
    },
    "instance_name": "",
    "expand_data_disk": 0,
    "reproduction_id": 0
  },
  "price_info": {
    "coupon_id_list": [],
    "machine_id": "b7f3c9a2",
    "charge_type": "payg",
    "duration": 1,
    "num": 1,
    "expand_data_disk": 0
  }
}
```

`expand_data_disk` 单位是字节（GB × 1024³）。

返回：

```json
{ "code": "Success", "data": { "uuid": "inst-xxxx" } }
```

> **这个请求客户端不做重试。** 超时即视为「结果不明」，
> 转成只读对账并停止下单，避免重复扣费。

### 实例相关

| 端点 | 方法 | 用途 |
| --- | --- | --- |
| `/api/v1/instance` | POST | 实例列表，请求体 `{page_index, page_size}` |
| `/api/v1/instance/exist?instance_uuid=` | GET | 实例是否已存在（对账用） |
| `/api/v1/instance/detail?instance_uuid=` | GET | 实例详情 |
| `/api/v1/instance/power_on` | POST | 开机，请求体 `{instance_uuid}` |
| `/api/v1/instance/power_off` | POST | 关机，请求体 `{instance_uuid}` |
| `/api/v1/instance/release` | POST | 释放实例，请求体 `{instance_uuid}` |
| `/api/v1/bill/confirm` | GET | 账单确认（可选步骤） |

### 其它

| 端点 | 方法 | 用途 |
| --- | --- | --- |
| `/api/v1/wallet` | GET | 控制台口径的钱包余额（官方接口的备用） |
| `/api/v1/region/list` | GET | 地区列表 |
| `/api/v1/machine/gpu_type` | GET | 全站 GPU 型号 |
| `/api/v1/machine/region/gpu_type` | POST | 某地区的 GPU 型号 |
| `/api/v1/machine/detail` | POST | 机器详情，请求体 `{machine_id}` |
| `/api/v1/image/get_framework` | GET | 官方镜像树 |
| `/api/v1/image/get` | POST | 镜像详情，请求体 `{image_uuid}` |

## 通知端点

| 端点 | 鉴权 | 请求体 |
| --- | --- | --- |
| `https://www.autodl.com/api/v1/wechat/message/send` | `Authorization: <微信消息 token>` | `{"title","name","content"}` |
| `https://sctapi.ftqq.com/<key>.send` | key 在 URL 里 | 表单 `title` / `desp` |

## 地区代码表

| 代码 | 名称 |
| --- | --- |
| `beijingDC1` | 北京A区 |
| `beijingDC2` | 北京B区 |
| `beijingDC3` | 北京C区 |
| `beijingDC4` | 北京D区 |
| `chongqingDC1` | 重庆A区 |
| `foshanDC1` | 佛山A区 |
| `neimengDC1` | 内蒙A区 |
| `neimengDC3` | 内蒙C区 |
| `westDC2` | 西北B区 |
| `westDC3` | 西北C区 |
| `yangzhouDC1` | 扬州A区 |

来源：AutoDL 官方文档《弹性部署 API》附录。
控制台实际返回以 `GET /api/v1/region/list` 为准；
本工具的 `regions.py` 里留了 `normalize_region()`，官方新增地区时无需改代码。

## 怎么改

所有端点集中在 `autodl_autoconnect/api.py` 顶部的 `ENDPOINTS` 字典，
官方改版时只改那一处。返回结构的解析层做了**多候选字段名兼容**
（见 `models.py`），字段改名时通常不用动。
