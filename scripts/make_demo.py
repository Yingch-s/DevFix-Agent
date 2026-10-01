"""生成 devfix 端到端演示场景：真实的 Maven + JUnit 5 Java 仓库。

用法（项目根目录）：
    .venv/Scripts/python scripts/make_demo.py --scenario simple --run-tests
    .venv/Scripts/python scripts/make_demo.py --scenario hard   --run-tests

产出（runs/e2e-demo/，已 gitignore）：
    repo/        带两次提交的 Maven 仓库
    build.log    真实 mvn test 的失败输出（需要 --run-tests）

场景：
    simple  单缺陷：重构时丢了 null 守卫 → NPE。一次修复即可通过。
    hard    副作用顺序陷阱：校验被删除且订单先落库。
            测试同时断言"抛业务异常"与"被拒绝的订单不得落库"——
            只在 NPE 处补 null 检查（最自然的改法）会导致第一个断言通过、
            第二个断言仍失败，必须由 Reflection 引导到"校验先于副作用"。
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

DEMO_ROOT = Path("runs/e2e-demo")
SRC = "src/main/java/com/example/order"
TEST = "src/test/java/com/example/order"

POM = """\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>order-service</artifactId>
  <version>1.0.0</version>

  <properties>
    <maven.compiler.source>21</maven.compiler.source>
    <maven.compiler.target>21</maven.compiler.target>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>

  <dependencies>
    <dependency>
      <groupId>org.junit.jupiter</groupId>
      <artifactId>junit-jupiter</artifactId>
      <version>5.10.2</version>
      <scope>test</scope>
    </dependency>
  </dependencies>

  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-surefire-plugin</artifactId>
        <version>3.2.5</version>
      </plugin>
    </plugins>
  </build>
</project>
"""

ORDER_REJECTED_EXCEPTION = """\
package com.example.order;

/** 业务拒绝：用户不存在或已删除时不允许下单。 */
public class OrderRejectedException extends RuntimeException {
    public OrderRejectedException(String message) {
        super(message);
    }
}
"""


# ====================================================================== simple
SIMPLE_FILES = {
    f"{SRC}/User.java": """\
package com.example.order;

public class User {
    private final Long id;
    private final boolean deleted;

    public User(Long id, boolean deleted) {
        this.id = id;
        this.deleted = deleted;
    }

    public Long getId() { return id; }

    public boolean isDeleted() { return deleted; }
}
""",
    f"{SRC}/Order.java": """\
package com.example.order;

public class Order {
    private final Long userId;

    public Order(Long userId) {
        this.userId = userId;
    }

    public Long getUserId() { return userId; }
}
""",
    f"{SRC}/UserRepository.java": """\
package com.example.order;

/** 用户查询：findById 返回任意用户，findActiveById 只返回未删除用户。 */
public interface UserRepository {
    User findById(Long id);

    User findActiveById(Long id);
}
""",
    f"{SRC}/OrderRejectedException.java": ORDER_REJECTED_EXCEPTION,
    # base：显式守卫
    f"{SRC}/OrderService.java": """\
package com.example.order;

public class OrderService {
    private final UserRepository userRepository;

    public OrderService(UserRepository userRepository) {
        this.userRepository = userRepository;
    }

    public Order createOrder(Long userId) {
        User user = userRepository.findById(userId);
        if (user == null || user.isDeleted()) {
            throw new OrderRejectedException("user not active: " + userId);
        }
        return new Order(user.getId());
    }
}
""",
    f"{TEST}/OrderServiceTest.java": """\
package com.example.order;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class OrderServiceTest {

    /** 已删除用户在仓库中查不到（返回 null）。 */
    private static class StubUserRepository implements UserRepository {
        @Override
        public User findById(Long id) {
            return new User(id, true);
        }

        @Override
        public User findActiveById(Long id) {
            return null;
        }
    }

    private final OrderService orderService = new OrderService(new StubUserRepository());

    @Test
    void shouldRejectDeletedUser() {
        assertThrows(OrderRejectedException.class, () -> orderService.createOrder(1L));
    }

    @Test
    void shouldCreateOrderForActiveUser() {
        OrderService service = new OrderService(new UserRepository() {
            @Override
            public User findById(Long id) { return new User(id, false); }

            @Override
            public User findActiveById(Long id) { return new User(id, false); }
        });
        assertEquals(7L, service.createOrder(7L).getUserId());
    }
}
""",
    # broken：丢失 null 守卫（第二次提交覆盖）
    f"{SRC}/OrderService.java#broken": """\
package com.example.order;

public class OrderService {
    private final UserRepository userRepository;

    public OrderService(UserRepository userRepository) {
        this.userRepository = userRepository;
    }

    public Order createOrder(Long userId) {
        User user = userRepository.findActiveById(userId);
        return new Order(user.getId());
    }
}
""",
}

SIMPLE_COMMITS = [
    ("base: order service with deleted-user guard", None),
    ("refactor: query active user directly", f"{SRC}/OrderService.java#broken"),
]


# ======================================================================== hard
HARD_FILES = {
    f"{SRC}/User.java": """\
package com.example.order;

public class User {
    private final Long id;
    private final String email;
    private final boolean deleted;

    public User(Long id, String email, boolean deleted) {
        this.id = id;
        this.email = email;
        this.deleted = deleted;
    }

    public Long getId() { return id; }

    public String getEmail() { return email; }

    public boolean isDeleted() { return deleted; }
}
""",
    f"{SRC}/Order.java": """\
package com.example.order;

public class Order {
    private final Long userId;
    private String buyerEmail;

    public Order(Long userId) {
        this.userId = userId;
    }

    public Long getUserId() { return userId; }

    public String getBuyerEmail() { return buyerEmail; }

    public void setBuyerEmail(String buyerEmail) { this.buyerEmail = buyerEmail; }
}
""",
    f"{SRC}/UserRepository.java": """\
package com.example.order;

/** 用户查询：findActiveById 只返回未删除用户，已删除用户返回 null。 */
public interface UserRepository {
    User findActiveById(Long id);
}
""",
    f"{SRC}/OrderRepository.java": """\
package com.example.order;

import java.util.List;

public interface OrderRepository {
    void save(Order order);

    List<Order> findAll();
}
""",
    f"{SRC}/OrderRejectedException.java": ORDER_REJECTED_EXCEPTION,
    # base：校验先于副作用
    f"{SRC}/OrderService.java": """\
package com.example.order;

public class OrderService {
    private final UserRepository userRepository;
    private final OrderRepository orderRepository;

    public OrderService(UserRepository userRepository, OrderRepository orderRepository) {
        this.userRepository = userRepository;
        this.orderRepository = orderRepository;
    }

    public Order createOrder(Long userId) {
        User user = userRepository.findActiveById(userId);
        if (user == null) {
            throw new OrderRejectedException("user not active: " + userId);
        }
        Order order = new Order(userId);
        orderRepository.save(order);
        order.setBuyerEmail(user.getEmail());
        return order;
    }
}
""",
    f"{TEST}/InMemoryOrderRepository.java": """\
package com.example.order;

import java.util.ArrayList;
import java.util.List;

/** 测试替身：内存订单仓库。 */
class InMemoryOrderRepository implements OrderRepository {
    private final List<Order> orders = new ArrayList<>();

    @Override
    public void save(Order order) {
        orders.add(order);
    }

    @Override
    public List<Order> findAll() {
        return List.copyOf(orders);
    }
}
""",
    f"{TEST}/OrderServiceTest.java": """\
package com.example.order;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class OrderServiceTest {

    /** 已删除用户在仓库中查不到（返回 null）。 */
    private static class DeletedUserRepository implements UserRepository {
        @Override
        public User findActiveById(Long id) {
            return null;
        }
    }

    private static class ActiveUserRepository implements UserRepository {
        @Override
        public User findActiveById(Long id) {
            return new User(id, "buyer@example.com", false);
        }
    }

    private final InMemoryOrderRepository orderRepository = new InMemoryOrderRepository();

    @Test
    void shouldRejectDeletedUser() {
        OrderService service = new OrderService(new DeletedUserRepository(), orderRepository);
        assertThrows(OrderRejectedException.class, () -> service.createOrder(1L));
    }

    @Test
    void shouldNotPersistRejectedOrder() {
        OrderService service = new OrderService(new DeletedUserRepository(), orderRepository);
        assertThrows(OrderRejectedException.class, () -> service.createOrder(1L));
        assertTrue(orderRepository.findAll().isEmpty(), "被拒绝的订单不应落库");
    }

    @Test
    void shouldPersistOrderForActiveUser() {
        OrderService service = new OrderService(new ActiveUserRepository(), orderRepository);
        service.createOrder(7L);
        assertEquals(1, orderRepository.findAll().size());
        assertEquals("buyer@example.com", orderRepository.findAll().get(0).getBuyerEmail());
    }
}
""",
    # broken：守卫被删，且副作用（落库）发生在校验之前
    f"{SRC}/OrderService.java#broken": """\
package com.example.order;

public class OrderService {
    private final UserRepository userRepository;
    private final OrderRepository orderRepository;

    public OrderService(UserRepository userRepository, OrderRepository orderRepository) {
        this.userRepository = userRepository;
        this.orderRepository = orderRepository;
    }

    public Order createOrder(Long userId) {
        User user = userRepository.findActiveById(userId);
        Order order = new Order(userId);
        orderRepository.save(order);
        order.setBuyerEmail(user.getEmail());
        return order;
    }
}
""",
}

HARD_COMMITS = [
    ("base: validate before persisting the order", None),
    ("refactor: simplify order creation flow", f"{SRC}/OrderService.java#broken"),
]

SCENARIOS = {
    "simple": (SIMPLE_FILES, SIMPLE_COMMITS),
    "hard": (HARD_FILES, HARD_COMMITS),
}


def _force_rmtree(path: Path) -> None:
    """Windows 下 .git/objects 内的文件是只读的，直接 rmtree 会 PermissionError。"""
    if not path.exists():
        return
    for p in path.rglob("*"):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        raise SystemExit(f"无法删除 {path}：请手动删除后重试（可能有进程占用）")


def _git(repo: Path, *args: str) -> None:
    r = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120, check=False,
    )
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{r.stderr}")


def build_repo(scenario: str) -> Path:
    files, commits = SCENARIOS[scenario]
    repo = DEMO_ROOT / "repo"
    if DEMO_ROOT.exists():
        _force_rmtree(DEMO_ROOT)

    (repo / SRC).mkdir(parents=True)
    (repo / TEST).mkdir(parents=True)
    (repo / "pom.xml").write_text(POM, encoding="utf-8")

    broken: list[tuple[Path, str]] = []
    for rel, content in files.items():
        path = repo / rel
        if rel.endswith("#broken"):
            broken.append((repo / rel.replace("#broken", ""), content))
            continue
        path.write_text(content, encoding="utf-8")

    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "devfix-demo")
    _git(repo, "config", "user.email", "demo@devfix.local")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", commits[0][0])

    for path, content in broken:
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", commits[1][0])
    return repo


def run_tests(repo: Path) -> None:
    """真实执行 mvn test，把输出保存为 build.log（失败日志即演示输入）。"""
    from devfix.tools import MavenTool

    print("正在执行 mvn test（首次运行需下载依赖，请稍候）…")
    raw = MavenTool(repo).run(["test"], timeout_seconds=900)
    (DEMO_ROOT / "build.log").write_text(raw.log, encoding="utf-8", newline="")
    print(f"mvn test 退出码：{raw.exit_code}（非 0 表示测试失败，正是演示所需）")
    print(f"耗时：{raw.duration_ms / 1000:.1f}s")
    print(f"日志已保存：{(DEMO_ROOT / 'build.log').resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="simple")
    parser.add_argument("--run-tests", action="store_true",
                        help="执行真实 mvn test 并生成 build.log")
    args = parser.parse_args()

    repo = build_repo(args.scenario)
    print(f"演示仓库已生成（{args.scenario} 场景）：{repo.resolve()}")
    if args.run_tests:
        run_tests(repo)
    print()
    print("运行修复闭环：")
    print(r"  .\.venv\Scripts\devfix.exe analyze runs\e2e-demo\repo "
          r"--log runs\e2e-demo\build.log --repair")


if __name__ == "__main__":
    if sys.platform == "win32":
        pass
    main()
