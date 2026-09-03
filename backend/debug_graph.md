mermaid
flowchart LR
    Request --> ClassifyRoute --> BasePricing --> CalculateFinalPrice --> PricingResult
```

# Nodes

## Request
type: input
position: 100, 200

## ClassifyRoute
type: expression
position: 400, 200
passThrough: true
content:
  expressions:
    - key: routeCategory
      value: "(routeDistance ?? 0) <= 1000 ? 'short' : (routeDistance ?? 0) <= 3000 ? 'medium' : 'long'"

## BasePricing
type: decisionTable
position: 700, 200
hitPolicy: first
passThrough: true
content:
  inputs:
    - id: cabin
      name: Cabin Class
      field: cabinClass
    - id: distance
      name: Route Category
      field: routeCategory
  outputs:
    - id: baseFare
      name: Base Fare
      field: baseFare
    - id: baggageAllowance
      name: Free Bags
      field: baggageAllowance
  rules:
    - cabin: "'First'"
      distance: '_'
      baseFare: 5000
      baggageAllowance: 4
    - cabin: "'Business'"
      distance: '_'
      baseFare: 3000
      baggageAllowance: 3
    - cabin: "'Premium Economy'"
      distance: '_'
      baseFare: 1500
      baggageAllowance: 2
    - cabin: "'Economy'"
      distance: "'short'"
      baseFare: 400
      baggageAllowance: 1
    - cabin: "'Economy'"
      distance: "'medium'"
      baseFare: 700
      baggageAllowance: 1
    - cabin: "'Economy'"
      distance: "'long'"
      baseFare: 1000
      baggageAllowance: 1

## CalculateFinalPrice
type: expression
position: 1000, 200
passThrough: true
content:
  expressions:
    - key: earlyBird
      value: "bookingDays >= 14 ? 0.10 : 0"
    - key: loyalty
      value: "loyaltyTier == 'Platinum' ? 0.15 : loyaltyTier == 'Gold' ? 0.10 : loyaltyTier == 'Silver' ? 0.05 : 0"
    - key: group
      value: "groupSize >= 10 ? 0.08 : 0"
    - key: bestDiscount
      value: "max([$.earlyBird, $.loyalty, $.group])"
    - key: discountCap
      value: "min($.bestDiscount, 0.25)"
    - key: discountAmount
      value: "baseFare * $.discountCap"
    - key: baggageFee
      value: "cabinClass == 'Economy' ? (checkedBags > baggageAllowance ? (checkedBags - baggageAllowance) * 30 : 0) : 0"
    - key: changeFee
      value: "isFlexFare ? 0 : cabinClass == 'Economy' ? 50 : 0"
    - key: seatFee
      value: "cabinClass == 'Economy' ? 20 : 10"
    - key: totalPrice
      value: "baseFare - $.discountAmount + $.baggageFee + $.changeFee + $.seatFee"

## PricingResult
type: output
position: 1300, 200