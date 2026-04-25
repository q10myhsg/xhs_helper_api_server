#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import time
import os
import sqlite3
import secrets
import base64
from datetime import datetime, timedelta
import traceback

# -------------------------- 配置 --------------------------
# API Key 三级权限配置
# CLIENT_API_KEYS: 客户端API Key，多个用逗号分隔
# ADMIN_API_KEY: 管理端API Key，仅管理员使用
CONFIG = {
    "admin_api_key": os.environ.get("ADMIN_API_KEY", ""),
    "client_api_keys": [k.strip() for k in os.environ.get("CLIENT_API_KEYS", "").split(",") if k.strip()],
    "rate_limit": int(os.environ.get("RATE_LIMIT", "60")),
    "db_path": os.environ.get("DB_PATH", "/tmp/activation.db"),
}

# 开发环境兼容，如果没配置客户端Key，默认允许test
if len(CONFIG["client_api_keys"]) == 0:
    CONFIG["client_api_keys"] = ["test"]

# -------------------------- 套餐配置 --------------------------
# 套餐配置（默认值）
PACKAGE_CONFIG = {
    "browser-extension": {
        "free": {
            "prompt_word": {"daily_limit": 20, "enable_like_filter": True},
            "download": {"daily_limit": 20},
            "search": {"high_value_notes": {"daily_limit": 30}, "keyword_expansion": {"daily_limit": 10}}
        },
        "basic": {
            "prompt_word": {"daily_limit": 50, "enable_like_filter": False},
            "download": {"daily_limit": 20},
            "search": {"high_value_notes": {"daily_limit": 100}, "keyword_expansion": {"daily_limit": 50}}
        },
        "premium": {
            "prompt_word": {"daily_limit": 100, "enable_like_filter": False},
            "download": {"daily_limit": 50},
            "search": {"high_value_notes": {"daily_limit": 200}, "keyword_expansion": {"daily_limit": 100}}
        }
    },
    "pc-client": {
        "free": {
            "auto_use": {"device_count": 1, "device_time": 20},
            "pdf": {"daily_limit": 10},
            "cover": {"daily_limit": 5},
            "transfer": {"daily_limit": 10}
        },
        "basic": {
            "auto_use": {"device_count": 3, "device_time": 60},
            "pdf": {"daily_limit": 30},
            "cover": {"daily_limit": 30},
            "transfer": {"daily_limit": 30}
        },
        "premium": {
            "auto_use": {"device_count": -1, "device_time": -1},
            "pdf": {"daily_limit": -1},
            "cover": {"daily_limit": -1},
            "transfer": {"daily_limit": -1}
        }
    }
}

# -------------------------- 数据存储 --------------------------
db_initialized = False
rate_limit_requests = {}
fallback_activation_codes = {}

def init_db():
    global db_initialized
    if db_initialized:
        return
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 检查是否已经有表，并且检查是否有client_type列
    cursor.execute("PRAGMA table_info(activation_codes)")
    columns = cursor.fetchall()
    has_client_type = any(col[1] == 'client_type' for col in columns)
    
    if not has_client_type:
        # 旧表没有client_type列，需要重建
        # 重命名旧表
        cursor.execute("ALTER TABLE activation_codes RENAME TO activation_codes_old")
        # 创建新表
        cursor.execute('''
            CREATE TABLE activation_codes (
                auth_code TEXT PRIMARY KEY,
                duration INTEGER NOT NULL,
                package_type TEXT NOT NULL,
                client_type TEXT NOT NULL,
                generate_date TEXT NOT NULL,
                activated_date TEXT,
                machine_code TEXT,
                expiry_date TEXT
            )
        ''')
        # 复制数据，client_type设为空字符串（向后兼容）
        cursor.execute('''
            INSERT INTO activation_codes 
            (auth_code, duration, package_type, client_type, generate_date, activated_date, machine_code, expiry_date)
            SELECT auth_code, duration, package_type, '', generate_date, activated_date, machine_code, expiry_date
            FROM activation_codes_old
        ''')
        # 删除旧表
        cursor.execute("DROP TABLE activation_codes_old")
        conn.commit()
    else:
        # 已经有client_type列，不需要做任何事
        pass

    # 检查并创建 package_permissions 表（存储可配置的权限）
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='package_permissions'");
    table_exists = cursor.fetchone() is not None
    if not table_exists:
        cursor.execute('''
            CREATE TABLE package_permissions (
                client_type TEXT NOT NULL,
                package_type TEXT NOT NULL,
                permissions_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (client_type, package_type)
            )
        ''')
        # 插入默认权限配置
        now = datetime.utcnow().isoformat()
        # 默认权限配置
        default_permissions = {
            "browser-extension": {
                "free": {
                    "prompt_word": {"daily_limit": 20, "enable_like_filter": True},
                    "download": {"daily_limit": 20},
                    "search": {"high_value_notes": {"daily_limit": 30}, "keyword_expansion": {"daily_limit": 10}}
                },
                "basic": {
                    "prompt_word": {"daily_limit": 50, "enable_like_filter": False},
                    "download": {"daily_limit": 20},
                    "search": {"high_value_notes": {"daily_limit": 100}, "keyword_expansion": {"daily_limit": 50}}
                },
                "premium": {
                    "prompt_word": {"daily_limit": 100, "enable_like_filter": False},
                    "download": {"daily_limit": 50},
                    "search": {"high_value_notes": {"daily_limit": 200}, "keyword_expansion": {"daily_limit": 100}}
                }
            },
            "pc-client": {
                "free": {
                    "auto_use": {"device_count": 1, "device_time": 20},
                    "pdf": {"daily_limit": 10},
                    "cover": {"daily_limit": 5},
                    "transfer": {"daily_limit": 10}
                },
                "basic": {
                    "auto_use": {"device_count": 3, "device_time": 60},
                    "pdf": {"daily_limit": 30},
                    "cover": {"daily_limit": 30},
                    "transfer": {"daily_limit": 30}
                },
                "premium": {
                    "auto_use": {"device_count": -1, "device_time": -1},
                    "pdf": {"daily_limit": -1},
                    "cover": {"daily_limit": -1},
                    "transfer": {"daily_limit": -1}
                }
            }
        }
        for client_type, packages in default_permissions.items():
            for package_type, permissions in packages.items():
                cursor.execute('''
                    INSERT INTO package_permissions 
                    (client_type, package_type, permissions_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                ''', (client_type, package_type, json.dumps(permissions), now, now))
        conn.commit()
    
    conn.close()
    db_initialized = True

def get_db_connection():
    db_dir = os.path.dirname(CONFIG["db_path"])
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(CONFIG["db_path"])
    conn.row_factory = sqlite3.Row
    return conn

def get_activation_code(auth_code):
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM activation_codes WHERE auth_code = ?", (auth_code,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return dict(row)
    return None

def update_activation_code(auth_code, data):
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    set_clause = ", ".join([f"{k} = ?" for k in data.keys()])
    sql = f"UPDATE activation_codes SET {set_clause} WHERE auth_code = ?"
    params = list(data.values()) + [auth_code]
    cursor.execute(sql, params)
    conn.commit()
    conn.close()

def insert_activation_code(data):
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    columns = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    sql = f"INSERT INTO activation_codes ({columns}) VALUES ({placeholders})"
    cursor.execute(sql, list(data.values()))
    conn.commit()
    conn.close()

def delete_activation_code(auth_code):
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM activation_codes WHERE auth_code = ?", (auth_code,))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def list_activation_codes(page=1, page_size=20, status=None, client_type=None, package_type=None):
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    offset = (page - 1) * page_size
    
    # 构建查询条件
    conditions = []
    params = []
    
    if status:
        if status == "activated":
            conditions.append("machine_code IS NOT NULL")
        elif status == "unused":
            conditions.append("machine_code IS NULL")
        elif status == "expired":
            conditions.append("expiry_date IS NOT NULL")
    
    if client_type:
        conditions.append("client_type = ?")
        params.append(client_type)
    
    if package_type:
        conditions.append("package_type = ?")
        params.append(package_type)
    
    if len(conditions) > 0:
        where_clause = " WHERE " + " AND ".join(conditions)
    else:
        where_clause = ""
    
    sql = f"SELECT * FROM activation_codes {where_clause} ORDER BY generate_date DESC LIMIT ? OFFSET ?"
    params.extend([page_size, offset])
    
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    result = []
    for row in rows:
        item = dict(row)
        # 计算状态
        if not item.get("machine_code"):
            item["status"] = "unused"
        else:
            # 检查是否过期
            if item.get("expiry_date") and item["expiry_date"] != "9999-12-31T23:59:59":
                try:
                    expiry_str = item["expiry_date"]
                    if expiry_str.endswith('Z'):
                        expiry_str = expiry_str[:-1]
                    expiry = datetime.fromisoformat(expiry_str)
                    if datetime.utcnow() > expiry:
                        item["status"] = "expired"
                    else:
                        item["status"] = "used"
                except Exception:
                    item["status"] = "used"
            else:
                item["status"] = "used"
        result.append(item)
    
    # 统计总数
    count_sql = "SELECT COUNT(*) FROM activation_codes" + where_clause
    cursor.execute(count_sql, params[:-2])
    total = cursor.fetchone()[0]
    
    conn.close()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": result
    }

# -------------------------- 权限配置管理 --------------------------
def get_package_permission(client_type, package_type):
    """Get permission configuration for (client_type, package_type)
    Returns None if not configured in database
    """
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM package_permissions WHERE client_type = ? AND package_type = ?", (client_type, package_type))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    try:
        info = dict(row)
        permissions = json.loads(info["permissions_json"])
        return permissions
    except Exception:
        return None

def list_package_permissions():
    """List all permission configurations"""
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM package_permissions ORDER BY client_type, package_type")
    rows = cursor.fetchall()
    result = []
    for row in rows:
        info = dict(row)
        try:
            info["permissions"] = json.loads(info["permissions_json"])
            del info["permissions_json"]
        except Exception:
            info["permissions"] = None
        result.append(info)
    conn.close()
    return result

def set_package_permission(client_type, package_type, permissions):
    """Set or update permission configuration"""
    init_db()
    now = datetime.utcnow().isoformat()
    permissions_json = json.dumps(permissions)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    # Check if exists
    cursor.execute("SELECT 1 FROM package_permissions WHERE client_type = ? AND package_type = ?", (client_type, package_type))
    exists = cursor.fetchone() is not None
    
    if exists:
        # Update
        cursor.execute('''
            UPDATE package_permissions 
            SET permissions_json = ?, updated_at = ?
            WHERE client_type = ? AND package_type = ?
        ''', (permissions_json, now, client_type, package_type))
    else:
        # Insert
        cursor.execute('''
            INSERT INTO package_permissions 
            (client_type, package_type, permissions_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (client_type, package_type, permissions_json, now, now))
    
    conn.commit()
    conn.close()
    return True

def delete_package_permission(client_type, package_type):
    """Delete permission configuration"""
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM package_permissions WHERE client_type = ? AND package_type = ?", (client_type, package_type))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected > 0

# -------------------------- API Key 验证 --------------------------
def verify_api_key(request_api_key):
    """Verify API key and return role
    Returns:
        (role, is_valid)
        role: "admin" / "client" / None
        is_valid: bool
    """
    if not request_api_key:
        return None, False
    
    request_api_key = request_api_key.strip()
    
    # 检查管理端
    if request_api_key == CONFIG["admin_api_key"]:
        return "admin", True
    
    # 检查客户端
    if request_api_key in CONFIG["client_api_keys"]:
        return "client", True
    
    return None, False

# -------------------------- 加密/随机生成 --------------------------
def generate_random_code(length=20):
    """生成指定长度的随机字母数字字符串"""
    chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
    return ''.join(secrets.choice(chars) for _ in range(length))

# -------------------------- 频率限制 --------------------------
def check_rate_limit(client_ip):
    now = int(time.time())
    window_start = now - 60
    if client_ip not in rate_limit_requests:
        rate_limit_requests[client_ip] = []
    # 清理60秒前的过期记录，防止内存增长
    requests = [t for t in rate_limit_requests[client_ip] if t > window_start]
    if len(requests) >= CONFIG["rate_limit"]:
        return False
    requests.append(now)
    rate_limit_requests[client_ip] = requests
    return True

# -------------------------- 接口处理 --------------------------
def handle_verify(body):
    auth_code = body.get("auth_code")
    machine_code = body.get("machine_code")
    client_type = body.get("client_type")
    plugin_version = body.get("plugin_version")
    
    if not auth_code or not machine_code or not client_type or not plugin_version:
        return {
            "status": "invalid",
            "message": "缺少必填参数 auth_code / machine_code / client_type / plugin_version",
            "data": None
        }
    
    # 验证 client_type 有效值
    if client_type not in ["browser-extension", "pc-client"]:
        return {
            "status": "invalid",
            "message": "client_type 必须是 'browser-extension' 或 'pc-client'",
            "data": None
        }
    
    parts = auth_code.split('_', 1)
    if len(parts) != 2:
        return {
            "status": "invalid",
            "message": "激活码格式错误",
            "data": None
        }
    
    try:
        info = get_activation_code(auth_code)
        if not info:
            return {
                "status": "invalid",
                "message": "激活码不存在",
                "data": None
            }
    except Exception:
        traceback.print_exc()
        if auth_code in fallback_activation_codes:
            info = fallback_activation_codes[auth_code]
        else:
            return {
                "status": "invalid",
                "message": "激活码不存在，数据库异常",
                "data": None
            }
    
    if info.get("machine_code") and info["machine_code"] != machine_code:
        return {
            "status": "used",
            "message": "激活码已被其他设备绑定",
            "data": None
        }
    
    # 如果这个激活码已经绑定到当前设备，直接验证过期就行
    # 如果同一个设备激活多次（不同激活码），我们允许绑定，查询时会返回最新的
    
    now = datetime.utcnow()
    current_expiry = body.get("current_expiry_date")
    
    # 如果已使用，验证 current_expiry_date 是否与存储的过期时间一致
    if info.get("machine_code") is not None:
        # 已使用过，验证客户端提供的过期时间是否与存储一致
        if current_expiry is not None and current_expiry != info.get("expiry_date"):
            return {
                "status": "invalid",
                "message": "激活信息已失效，请重新验证",
                "data": None
            }
        # 检查是否已过期
        if info.get("expiry_date"):
            try:
                expiry_str = info["expiry_date"]
                if expiry_str.endswith('Z'):
                    expiry_str = expiry_str[:-1]
                expiry = datetime.fromisoformat(expiry_str)
                if now > expiry:
                    return {
                        "status": "expired",
                        "message": "激活码已过期",
                        "data": None
                    }
            except Exception as e:
                traceback.print_exc()
                return {
                    "status": "invalid",
                    "message": f"过期时间格式错误: {info.get('expiry_date')}, error: {str(e)}",
                    "data": None
                }
    
    if not info.get("machine_code"):
        # 未使用过，首次激活，需要计算过期时间
        info["machine_code"] = machine_code
        info["activated_date"] = datetime.utcnow().isoformat()
        duration = info.get("duration")
        
        # 检查这个设备是否已经有其他激活码，累计有效期
        try:
            init_db()
            conn = get_db_connection()
            cursor = conn.cursor()
            # 查找当前设备+客户端已绑定的其他激活码
            cursor.execute("SELECT expiry_date FROM activation_codes WHERE machine_code = ? AND client_type = ? AND auth_code != ? AND expiry_date IS NOT NULL", 
                         (machine_code, client_type, auth_code))
            rows = cursor.fetchall()
            conn.close()
            
            # 找到最新的未过期过期时间，累计有效期
            latest_expiry = None
            for row in rows:
                expiry_str = row[0]
                if expiry_str == "9999-12-31T23:59:59":
                    # 已有永久激活，直接永久
                    latest_expiry = None
                    duration = -1
                    break
                if expiry_str:
                    if expiry_str.endswith('Z'):
                        expiry_str = expiry_str[:-1]
                    expiry_dt = datetime.fromisoformat(expiry_str)
                    if latest_expiry is None or expiry_dt > latest_expiry:
                        latest_expiry = expiry_dt
            
            # 计算最终过期时间
            if duration == -1:
                info["expiry_date"] = "9999-12-31T23:59:59"
            else:
                activated = datetime.utcnow()
                if latest_expiry and latest_expiry > activated:
                    # 累计有效期：原过期时间 + 新天数
                    expiry = latest_expiry + timedelta(days=duration)
                else:
                    # 没有已激活未过期的，从当前时间开始算
                    expiry = activated + timedelta(days=duration)
                info["expiry_date"] = expiry.isoformat()
        except Exception:
            traceback.print_exc()
            # 如果查询失败，回退到从当前时间计算
            duration = info.get("duration")
            if duration == -1:
                info["expiry_date"] = "9999-12-31T23:59:59"
            else:
                activated = datetime.utcnow()
                expiry = activated + timedelta(days=duration)
                info["expiry_date"] = expiry.isoformat()
        
        try:
            update_data = {
                "machine_code": info["machine_code"],
                "activated_date": info["activated_date"],
                "expiry_date": info["expiry_date"]
            }
            update_activation_code(auth_code, update_data)
        except Exception:
            traceback.print_exc()
            fallback_activation_codes[auth_code] = info
    
    # 如果激活码已经绑定到当前设备，保持原有的过期不变
    # 同一个设备允许多个激活码，查询时会选择最新未过期的
    
    return {
        "status": "valid",
        "message": "激活码验证成功",
        "data": {
            "expiry_date": info["expiry_date"],
            "activated_date": info["activated_date"],
            "machine_code": info["machine_code"]
        }
    }

def handle_generate(body):
    duration = body.get("duration")
    count = body.get("count", 1)
    count = min(max(1, count), 100)  # 限制一次最多生成100个，防止恶意打爆数据库
    package_type = body.get("package_type")
    client_type = body.get("client_type")
    
    if duration is None or not package_type or not client_type:
        return {
            "status": "error",
            "message": "缺少必填参数 duration 或 package_type 或 client_type",
            "data": None
        }
    
    # 验证 client_type 有效值
    if client_type not in ["browser-extension", "pc-client"]:
        return {
            "status": "error",
            "message": "client_type 必须是 'browser-extension' 或 'pc-client'",
            "data": None
        }
    
    # 允许任意整数duration，-1表示永久
    if not isinstance(duration, int) or (duration != -1 and duration <= 0):
        return {
            "status": "error",
            "message": "duration 必须是正整数 或 -1（永久）",
            "data": None
        }
    
    auth_codes = []
    generate_date = datetime.utcnow().isoformat()
    
    for _ in range(count):
        # 生成20位随机码，格式: {duration}_{20位随机}
        random_code = generate_random_code(20)
        auth_code = f"{duration}_{random_code}"
        
        # 存储数据
        info = {
            "auth_code": auth_code,
            "duration": duration,
            "package_type": package_type,
            "client_type": client_type,
            "generate_date": generate_date,
            "activated_date": None,
            "machine_code": None,
            "expiry_date": None
        }
        
        try:
            insert_activation_code(info)
        except Exception:
            traceback.print_exc()
            fallback_activation_codes[auth_code] = info
        
        auth_codes.append(auth_code)
    
    return {
        "status": "success",
        "message": f"成功生成 {count} 个激活码",
        "data": {
            "auth_codes": auth_codes,
            "duration": duration,
            "package_type": package_type,
            "client_type": client_type,
            "generate_date": generate_date
        }
    }

def handle_list(body):
    """列出激活码（管理接口）"""
    page = body.get("page", 1)
    page_size = body.get("page_size", 20)
    status = body.get("status")  # activated / unused / expired
    client_type = body.get("client_type")  # 按客户端类型筛选
    package_type = body.get("package_type")  # 按套餐类型筛选
    
    try:
        result = list_activation_codes(page, page_size, status, client_type, package_type)
        return {
            "status": "success",
            "message": "获取列表成功",
            "data": result
        }
    except Exception as e:
        traceback.print_exc()
        return {
            "status": "error",
            "message": f"获取列表失败: {str(e)}",
            "data": None
        }

def handle_delete(body):
    """删除激活码（管理接口）"""
    auth_code = body.get("auth_code")
    
    if not auth_code:
        return {
            "status": "error",
            "message": "缺少必填参数 auth_code",
            "data": {
                "deleted": False
            }
        }
    
    try:
        success = delete_activation_code(auth_code)
        if success:
            if auth_code in fallback_activation_codes:
                del fallback_activation_codes[auth_code]
            return {
                "status": "success",
                "message": f"激活码 {auth_code} 删除成功",
                "data": {
                    "deleted": True
                }
            }
        else:
            return {
                "status": "error",
                "message": "激活码不存在",
                "data": {
                    "deleted": False
                }
            }
    except Exception as e:
        traceback.print_exc()
        return {
            "status": "error",
            "message": f"删除失败: {str(e)}",
            "data": {
                "deleted": False
            }
        }

def handle_auth_info(body):
    """查询激活码信息接口
    查询激活码的详细信息，包括状态、绑定设备、过期时间等
    """
    auth_code = body.get("auth_code")
    
    if not auth_code:
        return {
            "status": "error",
            "message": "缺少必填参数 auth_code",
            "data": None
        }
    
    try:
        info = get_activation_code(auth_code)
        if not info:
            return {
                "status": "error",
                "message": "激活码不存在",
                "data": None
            }
        
        # 计算状态
        if not info.get("machine_code"):
            info["status"] = "unused"
        else:
            # 检查是否过期
            if info.get("expiry_date") and info["expiry_date"] != "9999-12-31T23:59:59":
                try:
                    expiry_str = info["expiry_date"]
                    if expiry_str.endswith('Z'):
                        expiry_str = expiry_str[:-1]
                    expiry = datetime.fromisoformat(expiry_str)
                    if datetime.utcnow() > expiry:
                        info["status"] = "expired"
                    else:
                        info["status"] = "used"
                except Exception:
                    info["status"] = "used"
            else:
                info["status"] = "used"
        
        return {
            "status": "success",
            "message": "查询成功",
            "data": info
        }
    except Exception as e:
        traceback.print_exc()
        return {
            "status": "error",
            "message": f"查询失败: {str(e)}",
            "data": None
        }

def handle_update(body):
    """更新激活码信息（管理接口）
    支持: 延期、换绑设备、修改过期时间等
    """
    auth_code = body.get("auth_code")
    update_data = body.get("update_data")
    
    if not auth_code or not update_data:
        return {
            "status": "error",
            "message": "缺少必填参数 auth_code 或 update_data",
            "data": None
        }
    
    try:
        info = get_activation_code(auth_code)
        if not info:
            return {
                "status": "error",
                "message": "激活码不存在",
                "data": None
            }
        
        # 不允许修改auth_code本身
        if "auth_code" in update_data:
            del update_data["auth_code"]
        
        # 如果解除绑定，清除machine_code和相关信息
        if update_data.get("unbind_machine"):
            update_data["machine_code"] = None
            update_data["activated_date"] = None
            update_data["expiry_date"] = None
            del update_data["unbind_machine"]
        
        # 如果更新了duration且已经激活，需要重新计算过期时间
        if "duration" in update_data and info.get("activated_date"):
            # 获取激活时间
            activated_str = info.get("activated_date")
            if activated_str:
                if activated_str.endswith('Z'):
                    activated_str = activated_str[:-1]
                activated = datetime.fromisoformat(activated_str)
                duration = update_data["duration"]
                if duration == -1:
                    update_data["expiry_date"] = "9999-12-31T23:59:59"
                else:
                    expiry = activated + timedelta(days=duration)
                    update_data["expiry_date"] = expiry.isoformat()
        
        update_activation_code(auth_code, update_data)
        info.update(update_data)
        
        return {
            "status": "success",
            "message": "更新成功",
            "data": info
        }
    except Exception as e:
        traceback.print_exc()
        return {
            "status": "error",
            "message": f"更新失败: {str(e)}",
            "data": None
        }

# -------------------------- 设备管理接口 --------------------------
def handle_device_info(body):
    """查询设备信息接口
    返回设备激活状态、过期时间、剩余天数等
    如果一个设备绑定了多个激活码，返回最新激活且未过期的那个
    """
    machine_code = body.get("machine_code")
    client_type = body.get("client_type")
    plugin_version = body.get("plugin_version")
    
    if not machine_code or not client_type or not plugin_version:
        return {
            "status": "error",
            "message": "缺少必填参数 machine_code / client_type / plugin_version",
            "data": None
        }
    
    # 验证 client_type 有效值
    if client_type not in ["browser-extension", "pc-client"]:
        return {
            "status": "error",
            "message": "client_type 必须是 'browser-extension' 或 'pc-client'",
            "data": None
        }
    
    try:
        init_db()
        conn = get_db_connection()
        cursor = conn.cursor()
        # 按激活时间倒序，返回最新的（按machine_code + client_type过滤）
        cursor.execute("SELECT * FROM activation_codes WHERE machine_code = ? AND client_type = ? ORDER BY activated_date DESC", (machine_code, client_type))
        rows = cursor.fetchall()
        conn.close()
        
        if len(rows) == 0:
            # 设备未记录
            return {
                "status": "success",
                "message": "查询成功",
                "data": {
                    "machine_code": machine_code,
                    "is_active": False,
                    "auth_code": None,
                    "package_type": None,
                    "activated_date": None,
                    "expiry_date": None,
                    "expired": False,
                    "days_remaining": 0,
                    "first_activation": True,
                    "last_verify_time": None,
                    "created_at": None
                }
            }
        
        # 找到第一个未过期的，如果都过期了返回最后一个
        selected_row = None
        for row in rows:
            info_candidate = dict(row)
            if info_candidate.get("expiry_date"):
                if info_candidate["expiry_date"] == "9999-12-31T23:59:59":
                    # 永久有效，选这个
                    selected_row = info_candidate
                    break
                try:
                    expiry_str = info_candidate["expiry_date"]
                    if expiry_str.endswith('Z'):
                        expiry_str = expiry_str[:-1]
                    expiry = datetime.fromisoformat(expiry_str)
                    if datetime.utcnow() <= expiry:
                        # 未过期，选这个
                        selected_row = info_candidate
                        break
                except Exception:
                    continue
        # 如果都过期了，还是选第一个（最新的）
        if selected_row is None:
            selected_row = dict(rows[0])
        
        info = selected_row
        is_active = info.get("machine_code") is not None and info.get("expiry_date") is not None
        expired = False
        days_remaining = 0
        
        if info.get("expiry_date") and info["expiry_date"] != "9999-12-31T23:59:59":
            try:
                expiry_str = info["expiry_date"]
                if expiry_str.endswith('Z'):
                    expiry_str = expiry_str[:-1]
                expiry = datetime.fromisoformat(expiry_str)
                now = datetime.utcnow()
                expired = now > expiry
                if not expired:
                    delta = expiry - now
                    days_remaining = delta.days
                else:
                    days_remaining = 0
            except Exception:
                expired = True
                days_remaining = 0
        elif info["expiry_date"] == "9999-12-31T23:59:59":
            # 永久有效
            days_remaining = -1
            expired = False
        
        first_activation = info.get("activated_date") is None or info.get("last_verify_time") is None
        
        # 默认权限（未激活）
        permissions = {
            "prompt_word": {
                "daily_limit": 20,
                "enable_like_filter": True
            },
            "download": {
                "daily_limit": 20
            },
            "search": {
                "high_value_notes": {
                    "daily_limit": 30
                },
                "keyword_expansion": {
                    "daily_limit": 10
                }
            }
        }
        
        if is_active and not expired and info:
            # 已激活，根据 client_type + package_type 从数据库获取权限
            package_type = info.get("package_type", "basic")
            ct = client_type
            db_permissions = get_package_permission(ct, package_type)
            if db_permissions is not None:
                permissions = db_permissions
            else:
                # 如果数据库没有配置，fallback到默认值
                if ct == "browser-extension":
                    if package_type == "free":
                        permissions = {
                            "prompt_word": {"daily_limit": 20, "enable_like_filter": True},
                            "download": {"daily_limit": 20},
                            "search": {"high_value_notes": {"daily_limit": 30}, "keyword_expansion": {"daily_limit": 10}}
                        }
                    elif package_type == "basic":
                        permissions = {
                            "prompt_word": {"daily_limit": 50, "enable_like_filter": False},
                            "download": {"daily_limit": 20},
                            "search": {"high_value_notes": {"daily_limit": 100}, "keyword_expansion": {"daily_limit": 50}}
                        }
                    elif package_type == "premium":
                        permissions = {
                            "prompt_word": {"daily_limit": 100, "enable_like_filter": False},
                            "download": {"daily_limit": 50},
                            "search": {"high_value_notes": {"daily_limit": 200}, "keyword_expansion": {"daily_limit": 100}}
                        }
                elif ct == "pc-client":
                    if package_type == "free":
                        permissions = {
                            "auto_use": {"device_count": 1, "device_time": 20},
                            "pdf": {"daily_limit": 10},
                            "cover": {"daily_limit": 5},
                            "transfer": {"daily_limit": 10}
                        }
                    elif package_type == "basic":
                        permissions = {
                            "auto_use": {"device_count": 3, "device_time": 60},
                            "pdf": {"daily_limit": 30},
                            "cover": {"daily_limit": 30},
                            "transfer": {"daily_limit": 30}
                        }
                    elif package_type == "premium":
                        permissions = {
                            "auto_use": {"device_count": -1, "device_time": -1},
                            "pdf": {"daily_limit": -1},
                            "cover": {"daily_limit": -1},
                            "transfer": {"daily_limit": -1}
                        }
        
        result = {
            "machine_code": info["machine_code"],
            "client_type": client_type,
            "is_active": is_active and not expired,
            "auth_code": info["auth_code"],
            "package_type": info.get("package_type"),
            "activated_date": info.get("activated_date"),
            "expiry_date": info.get("expiry_date"),
            "expired": expired,
            "days_remaining": days_remaining,
            "first_activation": first_activation,
            "last_verify_time": None,
            "created_at": info.get("activated_date"),
            "permissions": permissions
        }
        
        return {
            "status": "success",
            "message": "查询成功",
            "data": result
        }
    except Exception as e:
        traceback.print_exc()
        return {
            "status": "error",
            "message": f"查询失败: {str(e)}",
            "data": None
        }

def handle_device_list(body):
    """列出设备接口
    分页列出所有设备，支持按激活状态筛选
    """
    is_active = body.get("is_active")
    expired = body.get("expired")
    client_type = body.get("client_type")
    page = body.get("page", 1)
    page_size = body.get("page_size", 20)
    offset = (page - 1) * page_size
    
    try:
        init_db()
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 构建查询条件
        conditions = ["machine_code IS NOT NULL"]
        params = []
        
        if client_type:
            conditions.append("client_type = ?")
            params.append(client_type)
        
        where_clause = " WHERE " + " AND ".join(conditions) if len(conditions) > 0 else ""
        
        sql = f"SELECT * FROM activation_codes {where_clause} ORDER BY activated_date DESC LIMIT ? OFFSET ?"
        params.extend([page_size + 1, offset])
        
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        result_list = [dict(row) for row in rows]
        
        # 过滤
        filtered = []
        for item in result_list:
            match = True
            if is_active is not None:
                item_active = item.get("machine_code") is not None
                if item_active != is_active:
                    match = False
            if match and expired is not None:
                # 计算是否过期
                if item.get("expiry_date") and item["expiry_date"] != "9999-12-31T23:59:59":
                    try:
                        expiry_str = item["expiry_date"]
                        if expiry_str.endswith('Z'):
                            expiry_str = expiry_str[:-1]
                        expiry = datetime.fromisoformat(expiry_str)
                        item_expired = datetime.utcnow() > expiry
                    except Exception:
                        item_expired = True
                else:
                    item_expired = False
                if item_expired != expired:
                    match = False
            if match:
                filtered.append(item)
        
        # 统计总数
        cursor.execute("SELECT COUNT(*) FROM activation_codes WHERE machine_code IS NOT NULL")
        total = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            "status": "success",
            "message": "获取列表成功",
            "data": {
                "total": total,
                "page": page,
                "page_size": page_size,
                "items": filtered
            }
        }
    except Exception as e:
        traceback.print_exc()
        return {
            "status": "error",
            "message": f"获取列表失败: {str(e)}",
            "data": None
        }

def handle_device_unbind(body):
    """解绑设备接口
    解除设备与激活码的绑定，使激活码可以重新绑定
    """
    machine_code = body.get("machine_code")
    client_type = body.get("client_type")
    auth_code = body.get("auth_code")
    
    if not machine_code or not client_type:
        return {
            "status": "error",
            "message": "缺少必填参数 machine_code / client_type",
            "data": None
        }
    
    # 验证 client_type 有效值
    if client_type not in ["browser-extension", "pc-client"]:
        return {
            "status": "error",
            "message": "client_type 必须是 'browser-extension' 或 'pc-client'",
            "data": None
        }
    
    try:
        # 如果没提供auth_code，先查询绑定的auth_code
        if not auth_code:
            init_db()
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT auth_code FROM activation_codes WHERE machine_code = ? AND client_type = ?", (machine_code, client_type))
            row = cursor.fetchone()
            if not row:
                conn.close()
                return {
                    "status": "error",
                    "message": "设备未绑定任何激活码",
                    "data": {
                        "unbound": False
                    }
                }
            auth_code = row[0]
            conn.close()
        
        # 解绑：清除激活码上的机器绑定信息
        update_data = {
            "machine_code": None,
            "activated_date": None,
            "expiry_date": None
        }
        update_activation_code(auth_code, update_data)
        
        return {
            "status": "success",
            "message": "设备解绑成功，激活码可以重新绑定了",
            "data": {
                "unbound": True,
                "auth_code": auth_code
            }
        }
    except Exception as e:
        traceback.print_exc()
        return {
            "status": "error",
            "message": f"解绑失败: {str(e)}",
            "data": {
                "unbound": False
            }
        }

def handle_device_delete(body):
    """删除设备接口
    删除设备记录（解绑并清除记录）
    """
    machine_code = body.get("machine_code")
    client_type = body.get("client_type")
    
    if not machine_code or not client_type:
        return {
            "status": "error",
            "message": "缺少必填参数 machine_code / client_type",
            "data": None
        }
    
    # 验证 client_type 有效值
    if client_type not in ["browser-extension", "pc-client"]:
        return {
            "status": "error",
            "message": "client_type 必须是 'browser-extension' 或 'pc-client'",
            "data": None
        }
    
    try:
        init_db()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT auth_code FROM activation_codes WHERE machine_code = ? AND client_type = ?", (machine_code, client_type))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            return {
                "status": "success",
                "message": "设备不存在",
                "data": {
                    "deleted": False
                }
            }
        
        auth_code = row[0]
        cursor.execute("DELETE FROM activation_codes WHERE auth_code = ?", (auth_code,))
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        
        if auth_code in fallback_activation_codes:
            del fallback_activation_codes[auth_code]
        
        return {
            "status": "success",
            "message": f"设备 {machine_code} 删除成功",
            "data": {
                "deleted": affected > 0
            }
        }
    except Exception as e:
        traceback.print_exc()
        return {
            "status": "error",
            "message": f"删除失败: {str(e)}",
            "data": {
                "deleted": False
            }
        }

def handle_permissions_list(body):
    """列出所有权限配置（管理接口）"""
    try:
        result = list_package_permissions()
        return {
            "status": "success",
            "message": "获取列表成功",
            "data": {
                "total": len(result),
                "items": result
            }
        }
    except Exception as e:
        traceback.print_exc()
        return {
            "status": "error",
            "message": f"获取列表失败: {str(e)}",
            "data": None
        }

def handle_permissions_set(body):
    """设置或更新权限配置（管理接口）
    
    请求参数:
    - client_type: string - 客户端类型
    - package_type: string - 套餐类型
    - permissions: object - 完整权限配置JSON
    """
    client_type = body.get("client_type")
    package_type = body.get("package_type")
    permissions = body.get("permissions")
    
    if not client_type or not package_type or not permissions:
        return {
            "status": "error",
            "message": "缺少必填参数 client_type / package_type / permissions",
            "data": None
        }
    
    # 验证 client_type 有效值
    if client_type not in ["browser-extension", "pc-client"]:
        return {
            "status": "error",
            "message": "client_type 必须是 'browser-extension' 或 'pc-client'",
            "data": None
        }
    
    try:
        set_package_permission(client_type, package_type, permissions)
        return {
            "status": "success",
            "message": "权限配置更新成功",
            "data": {
                "client_type": client_type,
                "package_type": package_type,
                "permissions": permissions
            }
        }
    except Exception as e:
        traceback.print_exc()
        return {
            "status": "error",
            "message": f"更新失败: {str(e)}",
            "data": None
        }

def handle_package_config(body):
    """获取套餐配置接口
    
    请求参数: 无
    """
    try:
        # 构建返回数据：先从数据库读取，如果没有则用默认配置
        result = {}
        for client_type in PACKAGE_CONFIG:
            result[client_type] = {}
            for package_type in PACKAGE_CONFIG[client_type]:
                db_perm = get_package_permission(client_type, package_type)
                if db_perm is not None:
                    result[client_type][package_type] = db_perm
                else:
                    result[client_type][package_type] = PACKAGE_CONFIG[client_type][package_type]
        
        return {
            "status": "success",
            "message": "获取套餐配置成功",
            "data": result
        }
    except Exception as e:
        traceback.print_exc()
        return {
            "status": "error",
            "message": f"获取套餐配置失败: {str(e)}",
            "data": None
        }

def handle_permissions_delete(body):
    """删除权限配置（管理接口）
    
    请求参数:
    - client_type: string - 客户端类型
    - package_type: string - 套餐类型
    """
    client_type = body.get("client_type")
    package_type = body.get("package_type")
    
    if not client_type or not package_type:
        return {
            "status": "error",
            "message": "缺少必填参数 client_type / package_type",
            "data": None
        }
    
    try:
        deleted = delete_package_permission(client_type, package_type)
        if deleted:
            return {
                "status": "success",
                "message": "删除成功",
                "data": {
                    "deleted": True
                }
            }
        else:
            return {
                "status": "success",
                "message": "配置不存在，无需删除",
                "data": {
                    "deleted": False
                }
            }
    except Exception as e:
        traceback.print_exc()
        return {
            "status": "error",
            "message": f"删除失败: {str(e)}",
            "data": None
        }

# -------------------------- 主入口 --------------------------
def main_handler(event, context):
    headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-API-Key"
    }
    
    client_ip = event.get("requestContext", {}).get("sourceIp", 
                event.get("headers", {}).get("x-forwarded-for", "127.0.0.1"))
    
    if not check_rate_limit(client_ip):
        return {
            "statusCode": 429,
            "headers": headers,
            "body": json.dumps({
                "status": "error",
                "message": "请求过于频繁，请稍后再试",
                "data": None
            })
        }
    
    http_method = event.get("httpMethod", "").upper()
    if http_method == "OPTIONS":
        return {
            "statusCode": 200,
            "headers": headers,
            "body": ""
        }
    
    try:
        body = event.get("body")
        if event.get("isBase64Encoded") and body:
            body = base64.b64decode(body).decode('utf-8')
        if isinstance(body, str):
            body = json.loads(body)
    except Exception as e:
        traceback.print_exc()
        return {
            "statusCode": 400,
            "headers": headers,
            "body": json.dumps({
                "status": "error",
                "message": f"请求体解析错误: {str(e)}",
                "data": None
            }, ensure_ascii=False)
        }
    
    # 兼容大小写不同的header名称
    request_api_key = None
    for header_name, header_value in event.get("headers", {}).items():
        if header_name.lower() == "x-api-key":
            request_api_key = header_value
            break
    if not request_api_key:
        request_api_key = event.get("queryString", {}).get("apiKey", 
                        body.get("apiKey"))
    
    # 调试：打印配置信息，方便排查（不打印完整API Key）
    def mask_api_key(key):
        if not key:
            return ''
        if len(key) <= 8:
            return '*' * len(key)
        return key[:4] + '...' + key[-4:]
    
    print(f"ADMIN_API_KEY configured: {len(CONFIG['admin_api_key']) > 0}")
    print(f"CLIENT_API_KEYS count: {len(CONFIG['client_api_keys'])}")
    if request_api_key:
        print(f"Request API Key: {mask_api_key(request_api_key)}")
    
    role, valid = verify_api_key(request_api_key)
    if not valid:
        return {
            "statusCode": 401,
            "headers": headers,
            "body": json.dumps({
                "status": "error",
                "message": "API Key 无效",
                "debug": {
                    "admin_api_key_configured": len(CONFIG["admin_api_key"]) > 0,
                    "client_api_keys_count": len(CONFIG["client_api_keys"])
                },
                "data": None
            }, ensure_ascii=False)
        }
    
    try:
        path = event.get("path", "")
        result = None
        
        # 权限检查：管理接口需要admin role
        def requires_admin():
            if role != "admin":
                return {
                    "statusCode": 403,
                    "headers": headers,
                    "body": json.dumps({
                        "status": "error",
                        "message": "Forbidden: this endpoint requires admin role",
                        "data": None
                    }, ensure_ascii=False)
                }
            return None
        
        if path.endswith("/auth/verify") and http_method == "POST":
            # 允许client/admin访问
            result = handle_verify(body)
        elif path.endswith("/auth/generate") and http_method == "POST":
            check = requires_admin()
            if check:
                return check
            result = handle_generate(body)
        elif path.endswith("/auth/info") and http_method == "POST":
            check = requires_admin()
            if check:
                return check
            result = handle_auth_info(body)
        elif path.endswith("/auth/list") and http_method == "POST":
            check = requires_admin()
            if check:
                return check
            result = handle_list(body)
        elif path.endswith("/auth/delete") and http_method == "POST":
            check = requires_admin()
            if check:
                return check
            result = handle_delete(body)
        elif path.endswith("/auth/update") and http_method == "POST":
            check = requires_admin()
            if check:
                return check
            result = handle_update(body)
        elif path.endswith("/device/info") and http_method == "POST":
            # 允许client/admin访问
            result = handle_device_info(body)
        elif path.endswith("/device/list") and http_method == "POST":
            check = requires_admin()
            if check:
                return check
            result = handle_device_list(body)
        elif path.endswith("/device/unbind") and http_method == "POST":
            check = requires_admin()
            if check:
                return check
            result = handle_device_unbind(body)
        elif path.endswith("/device/delete") and http_method == "POST":
            check = requires_admin()
            if check:
                return check
            result = handle_device_delete(body)
        elif path.endswith("/permissions/list") and http_method == "POST":
            check = requires_admin()
            if check:
                return check
            result = handle_permissions_list(body)
        elif path.endswith("/permissions/set") and http_method == "POST":
            check = requires_admin()
            if check:
                return check
            result = handle_permissions_set(body)
        elif path.endswith("/permissions/delete") and http_method == "POST":
            check = requires_admin()
            if check:
                return check
            result = handle_permissions_delete(body)
        elif path.endswith("/package/config") and http_method == "POST":
            # 允许client/admin访问
            result = handle_package_config(body)
        else:
            return {
                "statusCode": 404,
                "headers": headers,
                "body": json.dumps({
                    "status": "error",
                    "message": "接口不存在",
                    "data": None
                })
            }
        
        return {
            "statusCode": 200,
            "headers": headers,
            "body": json.dumps(result, ensure_ascii=False)
        }
        
    except Exception as e:
        traceback.print_exc()
        return {
            "statusCode": 500,
            "headers": headers,
            "body": json.dumps({
                "status": "error",
                "message": f"服务器内部错误: {str(e)}",
                "data": None
            }, ensure_ascii=False)
        }
