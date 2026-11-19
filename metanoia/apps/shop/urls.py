"""HTML routes for the shop (mounted at ``/shop/`` under the ``shop`` namespace).

Sections, in order:

* **Storefront** (buyers) — the store and product pages.
* **Cart** — the cart, adding and updating lines, coupons; then downloads and
  the buyer's orders.
* **1-on-1s** — coaches, then one coach's week calendar. Booked sessions are
  listed on Online sessions; ``/shop/bookings/`` redirects there. The
  one-on-one track (:mod:`apps.shop.one_on_one`) adds hours, invoices and
  staff tools under ``one-on-one/``.
* **Catalogue management** (staff) — products, fulfilment, educators,
  discounts and categories under ``manage/``.
"""

from django.urls import path

from . import views, views_one_on_one

app_name = 'shop'

urlpatterns = [
    path('', views.storefront, name='storefront'),
    path('product/<int:pk>/', views.product_detail, name='product-detail'),

    path('cart/', views.cart, name='cart'),
    path('cart/add/<int:pk>/', views.add_to_cart, name='add-to-cart'),
    path('cart/item/<int:item_id>/', views.update_cart_item, name='update-cart-item'),
    path('cart/coupon/', views.apply_coupon, name='apply-coupon'),

    path('download/<int:pk>/', views.download_product, name='download-product'),
    path('download/<int:pk>/<int:file_id>/', views.download_file, name='download-file'),
    path('orders/', views.my_orders, name='my-orders'),
    path('one-on-one/', views.one_on_one, name='one-on-one'),
    path('one-on-one/<int:person_id>/', views.one_on_one_educator, name='one-on-one-educator'),
    path('one-on-one/hours/', views_one_on_one.hours, name='o2o-hours'),
    path('one-on-one/sessions/<int:pk>/cancel/', views_one_on_one.cancel, name='o2o-cancel'),
    path('one-on-one/students/', views_one_on_one.students, name='o2o-students'),
    path('one-on-one/students/<int:user_id>/', views_one_on_one.student, name='o2o-student'),
    path('bookings/', views.my_bookings, name='my-bookings'),
    path('bookings/<int:pk>/cancel/', views.cancel_booking, name='cancel-booking'),
    path('orders/<int:pk>/', views.order_detail, name='order-detail'),

    path('manage/products/', views.all_products, name='all-products'),
    path('manage/products/add/', views.add_product, name='add-product'),
    path('manage/products/<int:pk>/edit/', views.edit_product, name='edit-product'),
    path('manage/products/<int:pk>/delete/', views.delete_product, name='delete-product'),
    path('manage/fulfilment/', views.fulfilment, name='fulfilment'),
    path('manage/fulfilment/<int:pk>/', views.fulfilment_action, name='fulfilment-action'),
    path('manage/fulfilment/<int:pk>/waybill/', views.waybill, name='waybill'),
    path('manage/fulfilment/<int:pk>/slip/', views.packing_slip, name='packing-slip'),
    path('manage/educators/', views.educators, name='educators'),
    path('manage/educators/<int:person_id>/', views.educator_hours, name='educator-hours'),
    path('manage/discounts/', views.discounts, name='discounts'),
    path('manage/discounts/add/', views.add_discount, name='add-discount'),
    path('manage/discounts/<int:pk>/edit/', views.edit_discount, name='edit-discount'),
    path('manage/discounts/<int:pk>/delete/', views.delete_discount, name='delete-discount'),
    path('manage/categories/', views.categories, name='categories'),
    path('manage/categories/<int:pk>/delete/', views.delete_category, name='delete-category'),
]
