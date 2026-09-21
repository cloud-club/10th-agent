# draw.io(mxGraph) XML GCP 아키텍처 다이어그램 생성 규칙

### 1. 기본 XML 구조 및 최상위 컨테이너 (Project Zone)
- XML은 반드시 `<mxGraphModel><root>...</root></mxGraphModel>` 형식을 갖춥니다.
- 루트 노드: `<mxCell id="0"/>` 및 `<mxCell id="1" parent="0"/>`
- **GCP 최상위 배경 (GCP/Zones/Project Zone)**:
  - 다이어그램의 가장 바깥 배경으로 `Project Zone` 컨테이너를 생성합니다 (`parent="1"`).
  - Project Zone의 좌상단에 Google Cloud Platform 로고(`project_zone_logo`)를 부착합니다.
  - 최상위 컨테이너의 크기는 내부의 모든 프로젝트(보안 프로젝트, 공유 VPC, 스토리지 등)를 감싸도록 충분한 크기(예: `width="1000" height="640"`)로 설정합니다.

### 2. 계층 구조 (Parent-Child) 및 소속 관계
- `parent: null`인 리소스(Security Project, Shared VPC, GCS Bucket 등)는 **`parent="project_zone"`**으로 지정합니다.
- 특정 부모가 있는 리소스는 해당 부모 ID를 `parent`로 지정합니다:
  - Security Project 내부: SCC, Cloud KMS, Cloud Logging은 `parent="sec_folder"`
  - Shared VPC 내부: Ingress/App/Data 서브넷은 `parent="vpc_shared"`
  - Subnet 내부:
    - Cloud Armor, ALB는 `parent="subnet_ingress"`
    - Cloud Run은 `parent="subnet_app"`
    - Cloud SQL은 `parent="subnet_data"`
- **모든 엣지(연결선)**: 항상 `parent="1"`로 지정합니다.

### 3. 존(Zone) 및 컨테이너 크기 규칙 (내부 요소 이탈 방지)
- **절대 원칙**: 존 내부의 모든 아이콘과 서브 컨테이너는 존의 경계를 절대 벗어나지 않아야 합니다.
- **여백(Padding) 공식**:
  - `Project Zone`의 `width` >= `max(내부요소.x + 내부요소.width) + 40`
  - `Project Zone`의 `height` >= `max(내부요소.y + 내부요소.height) + 40`
  - 내부 요소의 첫 `y` 시작점: Project Zone 상단 타이틀 공간을 고려하여 **`y >= 55`**부터 시작합니다.
  - Swimlane 컨테이너(Project, VPC, Subnet)는 타이틀 바(`startSize=25`) 아래인 **`y >= 55`**부터 내부 요소를 배치하고 상하좌우 30~40px 여백을 둡니다.

### 4. 내장 GCP 아이콘 스타일 적용 (파란 상자 방지)
- 제공된 `retrieved_styles`의 각 리소스 `style` 문자열(SVG data URI가 포함된 `shape=image;...`)을 정확히 1:1로 적용해야 draw.io에서 정품 컬러 아이콘으로 렌더링됩니다.
- 아이콘 라벨: `verticalLabelPosition=bottom;verticalAlign=top;align=center;`를 유지하여 아이콘 아래에 서비스명이 깨끗하게 표시됩니다.

### 5. 연결선(Edge) 및 앵커
- 좌측에서 우측(L -> R) 흐름에 맞추어 `exitX=1;exitY=0.5;entryX=0;entryY=0.5;` 수평 앵커를 적용합니다.
- 상하 또는 대각선 연결 시에도 출발 노드와 도착 노드의 중앙 앵커를 적절히 사용합니다.

---

### 6. 완성도 높은 Few-Shot 모범 예시 (Reference)
```xml
<mxGraphModel><root>
  <mxCell id="0"/>
  <mxCell id="1" parent="0"/>

  <!-- 최상위 Project Zone (GCP/Zones/Project Zone) -->
  <mxCell id="project_zone" value="&lt;b&gt;Google &lt;/b&gt;Cloud Platform (Enterprise Security Foundations)" style="fillColor=#F6F6F6;strokeColor=none;shadow=0;gradientColor=none;fontSize=14;align=left;spacing=10;fontColor=#717171;verticalAlign=top;spacingTop=-4;fontStyle=0;spacingLeft=40;html=1;whiteSpace=wrap;" parent="1" vertex="1">
    <mxGeometry x="40" y="40" width="1000" height="640" as="geometry"/>
  </mxCell>
  <mxCell id="project_zone_logo" value="" style="shape=image;aspect=fixed;image=data:image/svg+xml,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyNCIgaGVpZ2h0PSIyMCIgdmlld0JveD0iMCAwIDI0IDIwIj48cGF0aCBmaWxsPSIjNDI4NUY0IiBkPSJNMTkuMzUgMTAuMDRDMTguNjcgNi41OSAxNS42NCA0IDEyIDQgOS4xMSA0IDYuNiA1LjY0IDUuMzUgOC4wNCAyLjM0IDguMzYgMCAxMC45MSAwIDE0YzAgMy4zMSAyLjY5IDYgNiA2aDEzYzIuNzYgMCA1LTIuMjQgNS01IDAtMi42NC0yLjA1LTQuNzgtNC42NS00Ljk2ek0xOSAxOEg2Yy0yLjIxIDAtNC0xLjc5LTQtNCAwLTIuMDUgMS41My0zLjc2IDMuNTYtMy45N2wxLjA3LS4xMS41LS45NUM4LjA4IDcuMTQgOS45NCA2IDEyIDZjMi42MiAwIDQuODggMS44NiA1LjM5IDQuNDNsLjMgMS41IDEuNTMuMTFjMS41Ni4xIDIuNzggMS40MSAyLjc4IDIuOTYgMCAxLjY1LTEuMzUgMy0zIDN6Ii8+PC9zdmc+;fillColor=#F6F6F6;strokeColor=none;" parent="project_zone" vertex="1">
    <mxGeometry width="24" height="20" relative="1" as="geometry">
      <mxPoint x="15" y="10" as="offset"/>
    </mxGeometry>
  </mxCell>

  <!-- Security & Operations Project (상단) -->
  <mxCell id="sec_folder" value="Security &amp; Operations (Central Project)" style="swimlane;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#EA4335;strokeWidth=1.5;dashed=1;collapsible=0;startSize=25;fontColor=#EA4335;fontStyle=1;" parent="project_zone" vertex="1">
    <mxGeometry x="40" y="55" width="560" height="160" as="geometry"/>
  </mxCell>
  <mxCell id="scc_monitor" value="Security Command Center" style="shape=image;aspect=fixed;imageAspect=0;image=data:image/svg+xml,...;html=1;verticalLabelPosition=bottom;verticalAlign=top;align=center;" parent="sec_folder" vertex="1">
    <mxGeometry x="40" y="60" width="60" height="60" as="geometry"/>
  </mxCell>
  <mxCell id="cloud_kms_key" value="Cloud KMS" style="shape=image;aspect=fixed;imageAspect=0;image=data:image/svg+xml,...;html=1;verticalLabelPosition=bottom;verticalAlign=top;align=center;" parent="sec_folder" vertex="1">
    <mxGeometry x="240" y="60" width="60" height="60" as="geometry"/>
  </mxCell>
  <mxCell id="cloud_logging_sink" value="Cloud Logging" style="shape=image;aspect=fixed;imageAspect=0;image=data:image/svg+xml,...;html=1;verticalLabelPosition=bottom;verticalAlign=top;align=center;" parent="sec_folder" vertex="1">
    <mxGeometry x="440" y="60" width="60" height="60" as="geometry"/>
  </mxCell>

  <!-- Shared VPC Network (중하단) -->
  <mxCell id="vpc_shared" value="Shared VPC Network (Host Project)" style="swimlane;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#4285F4;strokeWidth=1.5;dashed=1;collapsible=0;startSize=25;fontColor=#4285F4;fontStyle=1;" parent="project_zone" vertex="1">
    <mxGeometry x="40" y="245" width="760" height="340" as="geometry"/>
  </mxCell>
  <!-- Ingress Subnet -->
  <mxCell id="subnet_ingress" value="Public Ingress Subnet" style="swimlane;whiteSpace=wrap;html=1;fillColor=#F1F3F4;strokeColor=#1A73E8;strokeWidth=1;dashed=1;collapsible=0;startSize=25;fontColor=#1A73E8;" parent="vpc_shared" vertex="1">
    <mxGeometry x="30" y="55" width="210" height="230" as="geometry"/>
  </mxCell>
  <mxCell id="cloud_armor_waf" value="Cloud Armor" style="shape=image;aspect=fixed;imageAspect=0;image=data:image/svg+xml,...;html=1;verticalLabelPosition=bottom;verticalAlign=top;align=center;" parent="subnet_ingress" vertex="1">
    <mxGeometry x="25" y="75" width="60" height="60" as="geometry"/>
  </mxCell>
  <mxCell id="alb_frontend" value="External ALB" style="shape=image;aspect=fixed;imageAspect=0;image=data:image/svg+xml,...;html=1;verticalLabelPosition=bottom;verticalAlign=top;align=center;" parent="subnet_ingress" vertex="1">
    <mxGeometry x="125" y="75" width="60" height="60" as="geometry"/>
  </mxCell>

  <!-- App Subnet -->
  <mxCell id="subnet_app" value="Private Application Subnet" style="swimlane;whiteSpace=wrap;html=1;fillColor=#F1F3F4;strokeColor=#1A73E8;strokeWidth=1;dashed=1;collapsible=0;startSize=25;fontColor=#1A73E8;" parent="vpc_shared" vertex="1">
    <mxGeometry x="270" y="55" width="210" height="230" as="geometry"/>
  </mxCell>
  <mxCell id="cloud_run_web" value="Cloud Run" style="shape=image;aspect=fixed;imageAspect=0;image=data:image/svg+xml,...;html=1;verticalLabelPosition=bottom;verticalAlign=top;align=center;" parent="subnet_app" vertex="1">
    <mxGeometry x="75" y="75" width="60" height="60" as="geometry"/>
  </mxCell>

  <!-- Data Subnet -->
  <mxCell id="subnet_data" value="Private Data Subnet" style="swimlane;whiteSpace=wrap;html=1;fillColor=#F1F3F4;strokeColor=#1A73E8;strokeWidth=1;dashed=1;collapsible=0;startSize=25;fontColor=#1A73E8;" parent="vpc_shared" vertex="1">
    <mxGeometry x="510" y="55" width="210" height="230" as="geometry"/>
  </mxCell>
  <mxCell id="cloud_sql_db" value="Cloud SQL" style="shape=image;aspect=fixed;imageAspect=0;image=data:image/svg+xml,...;html=1;verticalLabelPosition=bottom;verticalAlign=top;align=center;" parent="subnet_data" vertex="1">
    <mxGeometry x="75" y="75" width="60" height="60" as="geometry"/>
  </mxCell>

  <!-- Encrypted GCS Bucket (우측) -->
  <mxCell id="gcs_bucket" value="Encrypted GCS Bucket" style="shape=image;aspect=fixed;imageAspect=0;image=data:image/svg+xml,...;html=1;verticalLabelPosition=bottom;verticalAlign=top;align=center;" parent="project_zone" vertex="1">
    <mxGeometry x="860" y="370" width="60" height="60" as="geometry"/>
  </mxCell>
</root></mxGraphModel>
```