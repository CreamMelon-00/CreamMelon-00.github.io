# 세계관 스토리 플레이어 배포용 키트

이 폴더는 정적 웹사이트로 바로 올릴 수 있는 구성입니다.

## 폴더 구조

index.html
data/storypack.json

`index.html`은 감상 전용 플레이어입니다.
`data/storypack.json`은 사이트가 자동으로 불러오는 작품 데이터입니다.

## 실제 작품으로 바꾸는 방법

1. 제작용 HTML 파일을 엽니다.
2. 작품, 에피소드, 배경, 고유명사 사전을 모두 정리합니다.
3. 로비에서 `프로젝트 내보내기`를 누릅니다.
4. 내려받은 `.storypack` 파일 이름을 `storypack.json`으로 바꿉니다.
5. 이 폴더의 `data/storypack.json`을 방금 만든 파일로 교체합니다.
6. 이 폴더 전체를 정적 호스팅 서비스에 업로드합니다.

## 빠른 테스트

로컬에서 그냥 index.html을 더블클릭하면 브라우저 보안 정책 때문에
data/storypack.json 자동 로드가 막힐 수 있습니다.

가장 쉬운 테스트는 폴더를 Netlify Drop이나 Cloudflare Pages Direct Upload에 올리는 것입니다.
또는 로컬에서 아래처럼 작은 서버를 켜고 확인할 수 있습니다.

python -m http.server 8000

그 다음 브라우저에서 아래 주소를 엽니다.

http://localhost:8000

## 주의

이 배포용 플레이어는 서버 없이 동작합니다.
방문자의 진행도는 방문자 자신의 브라우저 IndexedDB에 저장됩니다.
온라인 계정, 댓글, 서버 저장 같은 기능은 포함되어 있지 않습니다.
