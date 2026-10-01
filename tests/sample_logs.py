"""测试用 Maven 日志样本。

覆盖设计文档第 13 节的主要故障类型，格式对齐 surefire 3.x（JUnit 5）
与 surefire 2.x（JUnit 4）的真实输出。
"""

# ---- 单元测试失败 · JUnit 5 / surefire 3.x 新格式（带 [ERROR] 行前缀）----
JUNIT5_ASSERTION_FAILURE = """\
[INFO] -------------------------------------------------------
[INFO]  T E S T S
[INFO] -------------------------------------------------------
[INFO] Running com.example.OrderServiceTest
[ERROR] Tests run: 5, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 0.852 s <<< FAILURE! -- in com.example.OrderServiceTest
[ERROR] shouldRejectDeletedUser  Time elapsed: 0.045 s  <<< FAILURE!
[ERROR] java.lang.AssertionError: expected: <2> but was: <1>
[ERROR] 	at org.junit.jupiter.api.AssertEquals.failNotEqual(AssertEquals.java:197)
[ERROR] 	at com.example.OrderServiceTest.countActiveOrders(OrderServiceTest.java:47)
[INFO]
[INFO] Results:
[INFO]
[ERROR] Tests run: 5, Failures: 1, Errors: 0, Skipped: 0
[INFO]
[ERROR] There are test failures.
[INFO]
[INFO] BUILD FAILURE
"""

# ---- 运行时异常 · JUnit 4 / surefire 2.x 老格式（无行前缀 + Caused by 链）----
JUNIT4_NPE_ERROR = """\
[INFO] Running com.example.OrderServiceTest
Tests run: 3, Failures: 0, Errors: 1, Skipped: 0, Time elapsed: 0.12 sec <<< ERROR!
shouldRejectDeletedUser(com.example.OrderServiceTest)  Time elapsed: 0.045 sec  <<< ERROR!
java.lang.NullPointerException: Cannot invoke "com.example.User.getId()" because "user" is null
	at com.example.OrderService.createOrder(OrderService.java:82)
	at com.example.OrderServiceTest.shouldRejectDeletedUser(OrderServiceTest.java:47)
	at java.base/jdk.internal.reflect.NativeMethodAccessorImpl.invoke0(Native Method)
	at java.base/jdk.internal.reflect.NativeMethodAccessorImpl.invoke(NativeMethodAccessorImpl.java:77)
Caused by: java.lang.IllegalStateException: user not active
	at com.example.UserRepository.findActiveById(UserRepository.java:45)
	... 27 more
"""

# ---- 编译错误 · maven-compiler-plugin ----
COMPILATION_ERROR = """\
[INFO] -------------------------------------------------------------
[ERROR] COMPILATION ERROR
[INFO] -------------------------------------------------------------
[ERROR] /D:/Workspace/demo-projects/order-service/src/main/java/com/example/order/OrderController.java:[31,25] cannot find symbol
[ERROR]   symbol:   method findActiveById(int)
[ERROR]   location: variable userRepository of type com.example.order.UserRepository
[ERROR] /D:/Workspace/demo-projects/order-service/src/main/java/com/example/order/OrderService.java:[82,9] incompatible types: com.example.order.User cannot be converted to com.example.order.ActiveUser
[INFO] 2 errors
[INFO] -------------------------------------------------------------
[INFO] BUILD FAILURE
"""

# ---- 依赖解析错误 ----
DEPENDENCY_ERROR = """\
[INFO] ------------------------------------------------------------------------
[INFO] BUILD FAILURE
[INFO] ------------------------------------------------------------------------
[ERROR] Failed to execute goal on project order-service: Could not resolve dependencies for project com.example:order-service:jar:1.0.0: The following artifacts could not be resolved: com.example:payment-client:jar:1.2.0 (absent): Could not find artifact com.example:payment-client:jar:1.2.0 in central (https://repo.maven.apache.org/maven2)
[ERROR] -> [Help 1]
"""

# ---- Spring 上下文启动失败（集成测试阶段）----
SPRING_CONTEXT_FAILURE = """\
[INFO] Running com.example.OrderControllerIT
[ERROR] Tests run: 1, Failures: 0, Errors: 1, Skipped: 0, Time elapsed: 3.412 s <<< ERROR! -- in com.example.OrderControllerIT
[ERROR] contextLoads  Time elapsed: 0.001 s  <<< ERROR!
[ERROR] java.lang.IllegalStateException: Failed to load ApplicationContext
[ERROR] 	at org.springframework.test.context.cache.DefaultCacheAwareContextLoaderDelegate.loadContext(DefaultCacheAwareContextLoaderDelegate.java:132)
[ERROR] 	at com.example.OrderControllerIT.contextLoads(OrderControllerIT.java:19)
[ERROR] Caused by: org.springframework.beans.factory.UnsatisfiedDependencyException: Error creating bean with name 'orderController': Unsatisfied dependency expressed through constructor parameter 0: No qualifying bean of type 'com.example.order.PaymentClient' available
[ERROR] 	at org.springframework.beans.factory.support.ConstructorResolver.createArgumentArray(ConstructorResolver.java:800)
[ERROR] 	... 25 more
[INFO] BUILD FAILURE
"""

# ---- 真实 surefire 3.2.5 + JUnit 5 输出（从 runs/e2e-demo 的真实 mvn test 截取）----
# 失败头形态：全限定类名.方法名 -- Time elapsed ... <<< FAILURE!
SUREFIRE_32_FQ_FAILURE = """\
[INFO] Running com.example.order.OrderServiceTest
[ERROR] Tests run: 2, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 0.034 s <<< FAILURE! -- in com.example.order.OrderServiceTest
[ERROR] com.example.order.OrderServiceTest.shouldRejectDeletedUser -- Time elapsed: 0.005 s <<< FAILURE!
org.opentest4j.AssertionFailedError: Unexpected exception type thrown, expected: <com.example.order.OrderRejectedException> but was: <java.lang.NullPointerException>
\tat org.junit.jupiter.api.AssertionFailureBuilder.build(AssertionFailureBuilder.java:151)
\tat com.example.order.OrderServiceTest.shouldRejectDeletedUser(OrderServiceTest.java:27)
Caused by: java.lang.NullPointerException: Cannot invoke "com.example.order.User.getId()" because "user" is null
\tat com.example.order.OrderService.createOrder(OrderService.java:13)
[INFO] BUILD FAILURE
"""

# ---- 干净构建（无失败）----
CLEAN_BUILD = """\
[INFO] Tests run: 8, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 1.234 s
[INFO]
[INFO] Results:
[INFO]
[INFO] Tests run: 8, Failures: 0, Errors: 0, Skipped: 0
[INFO]
[INFO] BUILD SUCCESS
"""

# ---- 无关文本（应分类为 UNKNOWN）----
GARBAGE_LOG = """\
[INFO] Scanning for projects...
[INFO] Building order-service 1.0.0
[INFO] --- resources:3.3.1:resources (default-resources) @ order-service ---
[INFO] skip non existing resourceDirectory
"""
