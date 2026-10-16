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
| [acme_fitness_demo](https://github.com/vmwarecloudadvocacy/acme_fitness_demo) | 6/6 | 5/7 | – | 4/4 | 0 |
| [reddog-code](https://github.com/Azure/reddog-code) | 8/8 | 2/3 | 4/4 | 3/3 | 0 |
| [microservice-kubernetes](https://github.com/ewolff/microservice-kubernetes) | 4/4 | 5/5 | – | – | 0 |
| [ftgo-application](https://github.com/microservices-patterns/ftgo-application) | 4/4 | – | 3/3 | – | 16 |
| [yas](https://github.com/nashtech-garage/yas) | 16/16 | 15/15 | – | 9/9 | 68 |
| [eventuate-tram-sagas-examples-customers-and-orders](https://github.com/eventuate-tram/eventuate-tram-sagas-examples-customers-and-orders) | 2/3 | 0/2 | 1/1 | 2/2 | 2 |
| **Total** | **52/53** | **36/41** | **11/11** | **22/22** | 93 |

**Overall: 69/74 documented connections found (93%).**

## What was missed

- **acme_fitness_demo** (acmeshop.png): missed `catalog → cart`, `cart → order`
- **reddog-code** (README 'Architecture Diagram and Service Descriptions' + assets/reddog_code.png): missed `ui → order-service`
- **eventuate-tram-sagas-examples-customers-and-orders** (images/Eventuate_Tram_Customer_and_Order_Orchestration_Architecture.png): missed `api-gateway-service → customer-service`, `api-gateway-service → order-service`; services not found: ['api-gateway-service']

## Found but not in the docs

- **aks-store-demo**: `makeline-service → order-service`, `product-service → ai-service`, `store-admin → order-service`, `store-front → ai-service`, `virtual-customer → order-service`, `virtual-worker → makeline-service`, `virtual-worker → order-service`
- **ftgo-application**: `dynamodblocal-init → dynamodblocal`, `ftgo-accounting-service → cdc-service`, `ftgo-api-gateway → ftgo-consumer-service`, `ftgo-api-gateway → ftgo-order-history-service`, `ftgo-api-gateway → ftgo-order-service`, `ftgo-api-gateway-graphql → ftgo-consumer-service`, `ftgo-api-gateway-graphql → ftgo-order-history-service`, `ftgo-api-gateway-graphql → ftgo-restaurant-service`, `ftgo-consumer-service → cdc-service`, `ftgo-delivery-service → cdc-service`, `ftgo-kitchen-service → cdc-service`, `ftgo-order-history-service → cdc-service`, `ftgo-order-history-service → dynamodblocal`, `ftgo-order-history-service → dynamodblocal-init`, `ftgo-order-service → cdc-service`, `ftgo-restaurant-service → cdc-service`
- **yas**: `backoffice → backoffice-ui`, `backoffice → identity`, `backoffice → nginx`, `cart → identity`, `cart → media`, `cart → product`, `customer → identity`, `customer → location`, `identity → backoffice`, `identity → storefront`, `inventory → identity`, `inventory → location`, `inventory → product`, `keycloak → backoffice`, `keycloak → storefront`, `location → identity`, `media → identity`, `nginx → payment`, `nginx → pgadmin`, `nginx → promotion`, `nginx → recommendation`, `nginx → sampledata`, `nginx → swagger-ui`, `nginx → webhook`, `order → cart`, `order → customer`, `order → identity`, `order → product`, `order → promotion`, `order → tax`, `payment → identity`, `payment → media`, `payment → order`, `payment → storefront`, `product → identity`, `product → media`, `product → rating`, `promotion → identity`, `promotion → product`, `rating → customer`, `rating → identity`, `rating → order`, `rating → product`, `recommendation → customer`, `recommendation → order`, `recommendation → product`, `sampledata → identity`, `sampledata → media`, `sampledata → product`, `search → product`, `storefront → cart`, `storefront → customer`, `storefront → identity`, `storefront → nginx`, `storefront → storefront-ui`, `swagger-ui → cart`, `swagger-ui → customer`, `swagger-ui → inventory`, `swagger-ui → location`, `swagger-ui → media`, `swagger-ui → order`, `swagger-ui → payment`, `swagger-ui → product`, `swagger-ui → promotion`, `swagger-ui → rating`, `swagger-ui → tax`, `tax → identity`, `tax → location`
- **eventuate-tram-sagas-examples-customers-and-orders**: `api-gateway → customer-service`, `api-gateway → order-service`
