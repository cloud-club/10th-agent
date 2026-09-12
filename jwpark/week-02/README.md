### **1. 만들고 싶은 Agent 한 줄**

EKS Addon 관리 에이전트

자동으로 addon들의 버전을 관리한다.

### **2. Context 후보 2개**

Agent가 판단하기 위해 알아야 할 정보와 필요한 이유를 작성합니다.

- KubeAPI 서버와 통신
    - k8s api
    - 쿠버네티스 메트릭, 로그, 트레이스
- EKS add on release 정보
    - aws cli

### **3. Tool 후보 2개**

- k8sgpt
    - k8s 환경 진단.
- aws mcp 서버
    - https://docs.aws.amazon.com/ko_kr/agent-toolkit/latest/userguide/mcp-server.html
