# Generalization: the mapper on repos it was never built on

Mapper frozen at commit `c8078be` before these 8 repos were run. The answer key was written from each
repo's own README and architecture diagram before running, and the result is reported as it came out:
no mapper change was made to improve these numbers.

- **sync**: A calls B directly. **async**: A and B share a broker (a static map can only show both using it).
  **store**: A uses a datastore.
- "Not in docs" are calls the mapper found that the docs don't draw. Diagrams are often simplified, so these
  are not counted as wrong, but they are not counted as right either.

| Repo | Services found | Direct calls | Via broker | Datastore | Not in docs |
|---|---|---|---|---|---|
| [aks-store-demo](https://github.com/Azure-Samples/aks-store-demo) | 6/6 | 5/5 | 1/1 | 1/1 | 7 |
| [go-coffeeshop](https://github.com/thangchung/go-coffeeshop) | 6/6 | 4/4 | 2/2 | 3/3 | 0 |
| [acme_fitness_demo](https://github.com/vmwarecloudadvocacy/acme_fitness_demo) | 6/6 | 5/7 | – | 3/4 | 0 |
| [reddog-code](https://github.com/Azure/reddog-code) | 8/8 | 2/3 | 0/4 | 0/3 | 0 |
| [microservice-kubernetes](https://github.com/ewolff/microservice-kubernetes) | 4/4 | 5/5 | – | – | 0 |
| [ftgo-application](https://github.com/microservices-patterns/ftgo-application) | 4/4 | – | 3/3 | – | 16 |
| [yas](https://github.com/nashtech-garage/yas) | 15/16 | 14/15 | – | 9/9 | 39 |
| [eventuate-tram-sagas-examples-customers-and-orders](https://github.com/eventuate-tram/eventuate-tram-sagas-examples-customers-and-orders) | 0/3 | 0/2 | 0/1 | 0/2 | 3 |
| **Total** | **49/53** | **35/41** | **6/11** | **16/22** | 65 |

**Overall: 57/74 documented connections found (77%).**

## What was missed

- **acme_fitness_demo** (acmeshop.png): missed `catalog → cart`, `cart → order`, `user → store`
- **reddog-code** (README 'Architecture Diagram and Service Descriptions' + assets/reddog_code.png): missed `ui → order-service`, `order-service → make-line-service` (async), `order-service → loyalty-service` (async), `order-service → receipt-generation-service` (async), `order-service → accounting-service` (async), `make-line-service → store`, `loyalty-service → store`, `accounting-service → store`
- **yas** (yas-architecture-local.png (Storefront.BFF = `storefront`, Backoffice.BFF = `backoffice`)): missed `nginx → search`; services not found: ['search']
- **eventuate-tram-sagas-examples-customers-and-orders** (images/Eventuate_Tram_Customer_and_Order_Orchestration_Architecture.png): missed `api-gateway-service → customer-service`, `api-gateway-service → order-service`, `order-service → customer-service` (async), `customer-service → store`, `order-service → store`; services not found: ['api-gateway-service', 'customer-service', 'order-service']

## Found but not in the docs

- **aks-store-demo**: `makeline-service → order-service`, `product-service → ai-service`, `store-admin → order-service`, `store-front → ai-service`, `virtual-customer → order-service`, `virtual-worker → makeline-service`, `virtual-worker → order-service`
- **ftgo-application**: `dynamodblocal-init → dynamodblocal`, `ftgo-accounting-service → cdc-service`, `ftgo-api-gateway → ftgo-consumer-service`, `ftgo-api-gateway → ftgo-order-history-service`, `ftgo-api-gateway → ftgo-order-service`, `ftgo-consumer-service → cdc-service`, `ftgo-delivery-service → cdc-service`, `ftgo-kitchen-service → cdc-service`, `ftgo-order-history-service → cdc-service`, `ftgo-order-history-service → dynamodblocal`, `ftgo-order-history-service → dynamodblocal-init`, `ftgo-order-service → cdc-service`, `ftgo-order-service → ftgo-accounting-service`, `ftgo-order-service → ftgo-consumer-service`, `ftgo-order-service → ftgo-kitchen-service`, `ftgo-restaurant-service → cdc-service`
- **yas**: `backoffice → identity`, `backoffice → nginx`, `cart → identity`, `customer → identity`, `identity → backoffice`, `identity → storefront`, `inventory → identity`, `keycloak → backoffice`, `keycloak → storefront`, `location → identity`, `media → identity`, `nginx → payment`, `nginx → pgadmin`, `nginx → promotion`, `nginx → sampledata`, `order → identity`, `payment → identity`, `payment → storefront`, `product → identity`, `product → media`, `promotion → identity`, `rating → identity`, `sampledata → identity`, `sampledata → media`, `sampledata → product`, `storefront → identity`, `storefront → nginx`, `swagger-ui → cart`, `swagger-ui → customer`, `swagger-ui → inventory`, `swagger-ui → location`, `swagger-ui → media`, `swagger-ui → order`, `swagger-ui → payment`, `swagger-ui → product`, `swagger-ui → promotion`, `swagger-ui → rating`, `swagger-ui → tax`, `tax → identity`
- **eventuate-tram-sagas-examples-customers-and-orders**: `api-gateway-service-main → customer-service-main`, `api-gateway-service-main → order-service-main`, `order-service-main → customer-service-main`
